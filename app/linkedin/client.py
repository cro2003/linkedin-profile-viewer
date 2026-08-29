"""Client for LinkedIn's internal JSON API.

Two hard-won rules live here (see README limitations):

1. Cookies go in a *jar*, not a pinned `cookie` header. LinkedIn's edge rotates a
   routing cookie and answers with a 302 to the identical URL until it is echoed
   back, so a pinned header redirects forever.
2. The full harvested cookie set is required, not just the session cookie.
"""

import json
import logging

import httpx

from app.config import Account, settings
from app.linkedin.parse import primary_profile

log = logging.getLogger(__name__)

API_BASE = "https://www.linkedin.com/voyager/api"
PROFILE_PATH = "/identity/dash/profiles"
SESSION_PROBE_PATH = "/me"
PROFILE_DECORATION = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-91"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# a self-redirect loop means a stale jar, so fail fast instead of grinding
MAX_REDIRECTS = 3


class FetchError(Exception):
    retryable = False
    account_dead = False
    code = "fetch_failed"


class TransientError(FetchError):
    retryable = True
    code = "upstream_unavailable"


class PermanentError(FetchError):
    code = "profile_not_found"


class IdentityMismatch(PermanentError):
    """Upstream answered with a different person than the one requested.

    Observed live: an identifier LinkedIn cannot resolve does not 404, it returns
    some other profile. Serving that would hand the caller the wrong person's data,
    so it is refused rather than cached.
    """

    code = "profile_identity_mismatch"


class SessionExpired(FetchError):
    """Cookies no longer work; retry the job on a different account."""

    retryable = True
    account_dead = True
    code = "session_expired"


def headers_for(account: Account) -> dict[str, str]:
    return {
        "csrf-token": account.csrf_token,
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "x-restli-protocol-version": "2.0.0",
        "x-li-lang": "en_US",
        "x-li-track": json.dumps(
            {"clientVersion": "1.13.0", "osName": "web", "timezone": "Asia/Kolkata"}
        ),
        "user-agent": UA,
        "referer": "https://www.linkedin.com/feed/",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


def classify(status: int) -> type[FetchError] | None:
    """Map an HTTP status onto our retry policy. Pure, so it is unit-testable.

    403 is deliberately PermanentError here: LinkedIn returns it for a profile that
    does not exist as well as for a dead session, so `fetch_profile` disambiguates
    it with a session probe before trusting this mapping.
    """
    if status == 200:
        return None
    if status in (401, 999):
        return SessionExpired
    if status == 429 or status >= 500:
        return TransientError
    return PermanentError


class ProfileClient:
    def __init__(self, account: Account, transport: httpx.AsyncBaseTransport | None = None):
        self.account = account
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._proxy_disabled = False

    def _build(self) -> httpx.AsyncClient:
        proxy = None if self._proxy_disabled else self.account.proxy_url
        return httpx.AsyncClient(
            timeout=settings.request_timeout_sec,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            headers=headers_for(self.account),
            cookies=dict(self.account.cookies),
            proxy=None if self._transport else proxy,
            transport=self._transport,
        )

    async def __aenter__(self):
        self._client = self._build()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()

    @property
    def cookies(self) -> dict[str, str]:
        """Current jar, so the pool can persist rotated cookies.

        Read through the underlying cookiejar rather than dict(): the same name can
        be set for more than one domain (Cloudflare sets __cf_bm for both
        .linkedin.com and www.linkedin.com) and httpx's mapping raises on that.
        """
        if not self._client:
            return dict(self.account.cookies)
        # Overlay onto the jar we started with: cookies seeded into httpx carry an
        # empty domain, so filtering on domain alone drops every one of them.
        merged = dict(self.account.cookies)
        for c in self._client.cookies.jar:
            if not c.domain or "linkedin.com" in c.domain:
                merged[c.name] = c.value
        return merged

    async def _get(self, url: str) -> httpx.Response:
        try:
            return await self._client.get(url)
        except (httpx.ProxyError, httpx.ConnectError) as e:
            if self.account.proxy_url and not self._proxy_disabled and not settings.proxy_required:
                log.warning(
                    "proxy failed for %s (%s), falling back to direct",
                    self.account.id,
                    type(e).__name__,
                )
                self._proxy_disabled = True
                await self._client.aclose()
                self._client = self._build()
                return await self._client.get(url)
            raise TransientError(f"connect failed: {type(e).__name__}") from e
        except httpx.TooManyRedirects as e:
            raise SessionExpired("redirect loop — stale or incomplete cookie jar") from e
        except httpx.TimeoutException as e:
            raise TransientError("timeout") from e

    async def session_alive(self) -> bool:
        """Cheap liveness check, used only to disambiguate a 403."""
        try:
            r = await self._client.get(f"{API_BASE}{SESSION_PROBE_PATH}")
        except Exception:
            return False
        return r.status_code == 200

    async def fetch_profile(self, public_id: str) -> dict:
        url = (
            f"{API_BASE}{PROFILE_PATH}?q=memberIdentity"
            f"&memberIdentity={public_id}&decorationId={PROFILE_DECORATION}"
        )
        r = await self._get(url)

        if r.status_code == 403:
            if await self.session_alive():
                raise PermanentError(f"profile not found or not visible: {public_id}")
            raise SessionExpired(f"403 and session probe failed for {public_id}")

        error = classify(r.status_code)
        if error is not None:
            raise error(f"{r.status_code} for {public_id}")

        try:
            payload = r.json()
        except ValueError as e:
            # a 200 that is not JSON means we were handed a login/authwall page
            raise SessionExpired("200 but body is not JSON") from e

        entity = primary_profile(payload)
        if entity is None or not entity.get("publicIdentifier"):
            raise PermanentError(f"no profile in payload for {public_id}")

        returned = entity["publicIdentifier"]
        if returned.casefold() != public_id.casefold():
            raise IdentityMismatch(f"asked for {public_id!r}, upstream returned {returned!r}")
        return payload
