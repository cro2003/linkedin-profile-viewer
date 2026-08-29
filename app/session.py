"""Cookie re-minting via a persistent browser context.

Harvested cookie jars go stale within minutes, and a stale jar is indistinguishable
from a ban: the same endless self-redirect, and a browser bounce to the login page.
The recovery we observed is cheap — the *second* navigation logs straight back in,
because LinkedIn's remember-me cookie silently re-mints the session cookie.

So the refresh path is: keep an on-disk browser profile per account, load the feed,
retry the navigation once, harvest the jar. Credentials are a last resort, not the
normal path, because scripted logins are what draw a challenge.
"""

import asyncio
import logging
from pathlib import Path

from app.config import Account, settings
from app.db import redis
from app import accounts, pool

log = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"
LOGGED_OUT_MARKERS = ("/login", "/authwall", "/uas/login", "/checkpoint")

# The sign-in form has no stable ids: LinkedIn renders React-generated ones like
# «Rsvvriejj35659j6», and ships two copies of the form on the page. Input *types*
# are stable, so match on those, keep the older id/name variants as fallbacks, and
# take the visible copy.
USER_SELECTOR = ("#username:visible, input[name='session_key']:visible, "
                 "input[type='email']:visible")
PASS_SELECTOR = ("#password:visible, input[name='session_password']:visible, "
                 "input[type='password']:visible")
SUBMIT_SELECTOR = "button[type='submit']:visible"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

REFRESH_LOCK_TTL = 180


class SessionRefreshFailed(Exception):
    pass


class LoginCheckpointRequired(SessionRefreshFailed):
    """LinkedIn demanded human verification. Retrying cannot help."""
    pass


def _logged_out(url: str) -> bool:
    return any(marker in url for marker in LOGGED_OUT_MARKERS)


async def _harvest(account: Account) -> dict[str, str]:
    # imported lazily so the API image does not need a browser installed
    from playwright.async_api import async_playwright

    profile_dir = Path(settings.browser_profile_dir) / account.id
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=settings.browser_headless,
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            proxy={"server": account.proxy_url} if account.proxy_url else None,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(FEED_URL, wait_until="domcontentloaded")
            if _logged_out(page.url):
                # the remember-me cookie re-mints the session on a second attempt
                log.info("%s bounced to login, retrying navigation", account.id)
                await page.goto(FEED_URL, wait_until="domcontentloaded")

            if _logged_out(page.url):
                if not (account.email and account.password):
                    raise SessionRefreshFailed(
                        f"{account.id}: browser profile is logged out and no credentials configured")
                log.warning("%s falling back to credential login", account.id)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded")
                log.info("%s login page: url=%s title=%r", account.id, page.url,
                         await page.title())
                user_field = page.locator(USER_SELECTOR).first
                try:
                    await user_field.wait_for(timeout=15_000)
                except Exception as e:
                    # report what we were actually served, so this is diagnosable
                    # from logs alone instead of needing a live reproduction
                    inputs = await page.evaluate(
                        "() => [...document.querySelectorAll('input')]"
                        ".map(i => i.type + ':' + (i.id || i.name || '?'))")
                    log.error("%s inputs on page: %s", account.id, inputs)
                    raise SessionRefreshFailed(
                        f"{account.id}: no sign-in form at {page.url} "
                        f"(title={await page.title()!r}, inputs={inputs}); likely a bot "
                        f"check or an unrecognised login variant") from e
                await user_field.fill(account.email)
                password_field = page.locator(PASS_SELECTOR).first
                await password_field.fill(account.password)
                # submit with Enter rather than hunting for the button: the button
                # markup is as unstable as the input ids, the form submits on Enter
                await password_field.press("Enter")
                # settle on either a signed-in page or a challenge, whichever comes
                try:
                    await page.wait_for_url(
                        lambda u: not _logged_out(u) or "checkpoint" in u, timeout=45_000)
                except Exception as e:
                    raise SessionRefreshFailed(
                        f"{account.id}: login did not complete, still at {page.url}") from e

            if "checkpoint" in page.url or "challenge" in page.url:
                raise LoginCheckpointRequired(
                    f"{account.id}: LinkedIn requires human verification at {page.url}")

            cookies = {c["name"]: c["value"]
                       for c in await context.cookies("https://www.linkedin.com")}
            if not cookies.get("li_at") or not cookies.get("JSESSIONID"):
                raise SessionRefreshFailed(f"{account.id}: harvested jar is missing core cookies")
            return cookies
        finally:
            await context.close()


async def refresh(account: Account) -> dict[str, str]:
    """Re-mint and store this account's cookie jar.

    Guarded by a Redis lock so concurrent SessionExpired failures trigger one
    refresh, not one per in-flight job.
    """
    lock_key = f"acct:{account.id}:refresh_lock"
    if not await redis.set(lock_key, "1", nx=True, ex=REFRESH_LOCK_TTL):
        # someone else is refreshing; wait for their result rather than duplicating
        log.info("%s refresh already in progress, waiting", account.id)
        for _ in range(int(settings.cookie_refresh_timeout_sec)):
            await asyncio.sleep(1)
            if not await redis.exists(lock_key):
                stored = await accounts.get_account(account.id)
                return stored.cookies if stored else {}
        raise SessionRefreshFailed(f"{account.id}: timed out waiting on another refresh")

    try:
        await pool.set_status(account.id, pool.REFRESHING)
        cookies = await asyncio.wait_for(_harvest(account),
                                         timeout=settings.cookie_refresh_timeout_sec)
        await accounts.set_cookies(account.id, cookies)
        await pool.set_status(account.id, pool.LIVE)
        log.info("refreshed cookies for %s", account.id)
        return cookies
    except LoginCheckpointRequired as e:
        # park the account: further attempts only add failed logins to its record
        log.error("%s needs a human sign-in: %s", account.id, e)
        await pool.set_status(account.id, pool.NEEDS_LOGIN)
        raise
    except Exception as e:
        # a failed refresh is not automatically fatal; the account gets one more
        # chance on the next job before anything marks it dead
        log.error("cookie refresh failed for %s: %s", account.id, e)
        await pool.set_status(account.id, pool.LIVE)
        raise SessionRefreshFailed(str(e)) from e
    finally:
        await redis.delete(lock_key)
