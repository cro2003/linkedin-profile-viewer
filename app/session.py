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
from app import pool

log = logging.getLogger(__name__)

FEED_URL = "https://www.linkedin.com/feed/"
LOGIN_URL = "https://www.linkedin.com/login"
LOGGED_OUT_MARKERS = ("/login", "/authwall", "/uas/login", "/checkpoint")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

REFRESH_LOCK_TTL = 180


class SessionRefreshFailed(Exception):
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
                await page.fill("#username", account.email)
                await page.fill("#password", account.password)
                await page.click('button[type="submit"]')
                await page.wait_for_url(lambda u: not _logged_out(u), timeout=45_000)

            if "checkpoint" in page.url or "challenge" in page.url:
                raise SessionRefreshFailed(f"{account.id}: login checkpoint needs a human")

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
                return await pool.get_cookies(account.id)
        raise SessionRefreshFailed(f"{account.id}: timed out waiting on another refresh")

    try:
        await pool.set_status(account.id, pool.REFRESHING)
        cookies = await asyncio.wait_for(_harvest(account),
                                         timeout=settings.cookie_refresh_timeout_sec)
        await pool.set_cookies(account.id, cookies)
        await pool.set_status(account.id, pool.LIVE)
        log.info("refreshed cookies for %s", account.id)
        return cookies
    except Exception as e:
        # a failed refresh is not automatically fatal; the account gets one more
        # chance on the next job before anything marks it dead
        log.error("cookie refresh failed for %s: %s", account.id, e)
        await pool.set_status(account.id, pool.LIVE)
        raise SessionRefreshFailed(str(e)) from e
    finally:
        await redis.delete(lock_key)
