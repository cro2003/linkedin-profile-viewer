"""Account pool held in Redis.

Redis, not the process, owns account state so the API and every worker agree on
which accounts are usable and when each was last used. Two things matter here:

- **Serialisation.** One in-flight request per account, enforced by a lock. Parallel
  requests on a single account is what gets an account flagged.
- **Spacing.** A jittered delay after each use, so a burst never looks like a burst.

Cookie jars live here too rather than in the environment, because they rotate
constantly and every worker needs the current one.
"""

import json
import logging
import random
import time

from app.config import Account, settings
from app.db import redis

log = logging.getLogger(__name__)

LIVE, REFRESHING, DEAD = "live", "refreshing", "dead"

# lock TTL is a safety net: if a worker dies mid-fetch the account frees itself
LEASE_TTL_SEC = 120


def _k(account_id: str, suffix: str) -> str:
    return f"acct:{account_id}:{suffix}"


async def seed_from_env() -> None:
    """Seed Redis from .env for any account that has no jar yet. Existing jars win,
    since a refreshed jar is newer than whatever was in the environment."""
    for account in settings.linkedin_accounts:
        key = _k(account.id, "cookies")
        if not await redis.exists(key):
            await redis.set(key, json.dumps(account.cookies))
            log.info("seeded cookies for %s from env", account.id)
        if not await redis.exists(_k(account.id, "status")):
            await redis.set(_k(account.id, "status"), LIVE)


async def get_cookies(account_id: str) -> dict[str, str]:
    raw = await redis.get(_k(account_id, "cookies"))
    return json.loads(raw) if raw else {}


async def set_cookies(account_id: str, cookies: dict[str, str]) -> None:
    """Persist a jar, refusing one that would leave the account unusable.

    A partial jar is worse than no write at all: it destroys the working session
    and every later lease fails validation.
    """
    missing = [c for c in ("li_at", "JSESSIONID") if not cookies.get(c)]
    if missing:
        log.error("refusing to store jar for %s, missing %s", account_id, missing)
        return
    await redis.set(_k(account_id, "cookies"), json.dumps(cookies))
    log.info("stored %d cookies for %s", len(cookies), account_id)


async def set_status(account_id: str, status: str) -> None:
    await redis.set(_k(account_id, "status"), status)


async def get_status(account_id: str) -> str:
    return await redis.get(_k(account_id, "status")) or LIVE


def _configured(account_id: str) -> Account | None:
    return next((a for a in settings.linkedin_accounts if a.id == account_id), None)


async def lease() -> Account | None:
    """Take an available account, or None if every account is busy, cooling or dead.

    Shuffled so load spreads instead of always hammering the first account.
    """
    candidates = list(settings.linkedin_accounts)
    random.shuffle(candidates)

    for configured in candidates:
        if await get_status(configured.id) == DEAD:
            continue
        next_ok = await redis.get(_k(configured.id, "next_ok_at"))
        if next_ok and time.time() < float(next_ok):
            continue
        # NX lock == one in-flight request per account
        if not await redis.set(_k(configured.id, "lock"), "1", nx=True, ex=LEASE_TTL_SEC):
            continue

        cookies = await get_cookies(configured.id) or configured.cookies
        return Account(id=configured.id, cookies=cookies, proxy_url=configured.proxy_url)
    return None


async def release(account_id: str, *, cookies: dict[str, str] | None = None) -> None:
    """Free the account and start its cooldown. Persist a rotated jar if given."""
    if cookies:
        await set_cookies(account_id, cookies)
    delay = random.uniform(settings.account_min_delay_sec, settings.account_max_delay_sec)
    await redis.set(_k(account_id, "next_ok_at"), str(time.time() + delay))
    await redis.delete(_k(account_id, "lock"))


async def snapshot() -> list[dict]:
    """Per-account view for /health and /v1/stats."""
    out = []
    now = time.time()
    for account in settings.linkedin_accounts:
        next_ok = await redis.get(_k(account.id, "next_ok_at"))
        out.append({
            "id": account.id,
            "status": await get_status(account.id),
            "busy": bool(await redis.exists(_k(account.id, "lock"))),
            "cooldown_sec": max(0, round(float(next_ok) - now, 1)) if next_ok else 0,
            "has_cookies": bool(await get_cookies(account.id)),
            "proxy": bool(account.proxy_url),
        })
    return out
