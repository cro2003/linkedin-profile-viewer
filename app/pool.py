"""Account scheduling.

Redis, not the process, owns the *ephemeral* state — who is busy, who is cooling,
who is unusable — so the API and every worker agree. Two things matter here:

- **Serialisation.** One in-flight request per account, enforced by a lock. Parallel
  requests on a single account is what gets an account flagged.
- **Spacing.** A jittered delay after each use, so a burst never looks like a burst.

Account records and their cookie jars live in Mongo (app/accounts.py); nothing
durable is kept here.
"""

import logging
import random
import time

from app import accounts
from app.config import Account, settings
from app.db import redis

log = logging.getLogger(__name__)

LIVE, REFRESHING, DEAD = "live", "refreshing", "dead"
# a checkpoint (verification code, captcha) cannot be cleared by retrying: the
# account is out of action until a human signs in again
NEEDS_LOGIN = "needs_login"
UNUSABLE = (DEAD, NEEDS_LOGIN)

# lock TTL is a safety net: if a worker dies mid-fetch the account frees itself
LEASE_TTL_SEC = 120


def _k(account_id: str, suffix: str) -> str:
    return f"acct:{account_id}:{suffix}"


async def set_status(account_id: str, status: str) -> None:
    await redis.set(_k(account_id, "status"), status)


async def get_status(account_id: str) -> str:
    return await redis.get(_k(account_id, "status")) or LIVE


async def lease() -> Account | None:
    """Take an available account, or None if every account is busy, cooling or unusable.

    Shuffled so load spreads instead of always hammering the first account.
    """
    candidates = await accounts.list_accounts()
    random.shuffle(candidates)

    for account in candidates:
        if await get_status(account.id) in UNUSABLE:
            continue
        next_ok = await redis.get(_k(account.id, "next_ok_at"))
        if next_ok and time.time() < float(next_ok):
            continue
        # NX lock == one in-flight request per account
        if not await redis.set(_k(account.id, "lock"), "1", nx=True, ex=LEASE_TTL_SEC):
            continue
        return account
    return None


async def release(account_id: str, *, cookies: dict[str, str] | None = None) -> None:
    """Free the account and start its cooldown. Persist a rotated jar if given."""
    if cookies:
        await accounts.set_cookies(account_id, cookies)
    await accounts.touch(account_id)
    delay = random.uniform(settings.account_min_delay_sec, settings.account_max_delay_sec)
    await redis.set(_k(account_id, "next_ok_at"), str(time.time() + delay))
    await redis.delete(_k(account_id, "lock"))


async def snapshot() -> list[dict]:
    """Per-account view for /health and the admin panel."""
    out = []
    now = time.time()
    for doc in await accounts.list_docs():
        next_ok = await redis.get(_k(doc["id"], "next_ok_at"))
        out.append(
            {
                "id": doc["id"],
                "status": "disabled" if doc["disabled"] else await get_status(doc["id"]),
                "busy": bool(await redis.exists(_k(doc["id"], "lock"))),
                "cooldown_sec": max(0, round(float(next_ok) - now, 1)) if next_ok else 0,
                "has_cookies": doc["cookie_count"] > 0,
                "proxy": bool(doc["proxy_url"]),
                "last_used_at": doc["last_used_at"].isoformat() if doc["last_used_at"] else None,
            }
        )
    return out
