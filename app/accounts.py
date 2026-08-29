"""LinkedIn accounts, stored in Mongo.

Mongo is the single source of truth for an account and its cookie jar; Redis holds
only ephemeral scheduling state (see app/pool.py). Keeping the jar in one place
avoids the two stores disagreeing about which cookies are current.

Accounts configured in the environment are seeded once, so an existing deployment
keeps working after this moved out of .env.
"""

import logging
from datetime import UTC, datetime

from app.config import Account, settings
from app.db import db

log = logging.getLogger(__name__)

collection = db.linkedin_accounts
REQUIRED_COOKIES = ("li_at", "JSESSIONID")


def _now() -> datetime:
    return datetime.now(UTC)


def _to_account(doc: dict) -> Account:
    return Account(
        id=doc["_id"],
        cookies=doc.get("cookies") or {},
        proxy_url=doc.get("proxy_url"),
        email=doc.get("email"),
        password=None,  # never persisted; present on the model for transient use
    )


async def ensure_indexes() -> None:
    await collection.create_index("disabled")


async def seed_from_env() -> None:
    """One-time migration: copy accounts out of the environment on first boot."""
    for account in settings.linkedin_accounts:
        existing = await collection.find_one({"_id": account.id})
        if existing:
            continue
        await collection.insert_one(
            {
                "_id": account.id,
                "cookies": account.cookies,
                "proxy_url": account.proxy_url,
                "email": account.email,
                "disabled": False,
                "created_at": _now(),
                "last_used_at": None,
                "note": "seeded from environment",
            }
        )
        log.info("seeded account %s from environment", account.id)


async def list_accounts(*, include_disabled: bool = False) -> list[Account]:
    query = {} if include_disabled else {"disabled": {"$ne": True}}
    out = []
    async for doc in collection.find(query):
        try:
            out.append(_to_account(doc))
        except Exception as e:
            # one broken document must not take the whole pool offline
            log.warning("skipping unusable account %s: %s", doc.get("_id"), e)
    return out


async def list_docs() -> list[dict]:
    """Raw documents for the admin panel, without cookie values."""
    out = []
    async for doc in collection.find({}):
        out.append(
            {
                "id": doc["_id"],
                "proxy_url": doc.get("proxy_url"),
                "disabled": bool(doc.get("disabled")),
                "email": doc.get("email"),
                "note": doc.get("note"),
                "created_at": doc.get("created_at"),
                "last_used_at": doc.get("last_used_at"),
                "cookie_count": len(doc.get("cookies") or {}),
            }
        )
    return out


async def get_account(account_id: str) -> Account | None:
    doc = await collection.find_one({"_id": account_id})
    return _to_account(doc) if doc else None


async def set_cookies(account_id: str, cookies: dict[str, str]) -> bool:
    """Persist a jar, refusing one that would leave the account unusable.

    A partial jar is worse than no write at all: it destroys a working session and
    every later lease fails validation.
    """
    missing = [c for c in REQUIRED_COOKIES if not cookies.get(c)]
    if missing:
        log.error("refusing to store jar for %s, missing %s", account_id, missing)
        return False
    await collection.update_one(
        {"_id": account_id}, {"$set": {"cookies": cookies, "cookies_updated_at": _now()}}
    )
    log.info("stored %d cookies for %s", len(cookies), account_id)
    return True


async def create(
    account_id: str,
    cookies: dict[str, str],
    *,
    proxy_url: str | None = None,
    email: str | None = None,
    note: str | None = None,
) -> bool:
    missing = [c for c in REQUIRED_COOKIES if not cookies.get(c)]
    if missing:
        raise ValueError(f"cookies missing {missing}")
    await collection.update_one(
        {"_id": account_id},
        {
            "$set": {
                "cookies": cookies,
                "proxy_url": proxy_url,
                "email": email,
                "note": note,
                "cookies_updated_at": _now(),
            },
            "$setOnInsert": {"disabled": False, "created_at": _now(), "last_used_at": None},
        },
        upsert=True,
    )
    return True


async def update(account_id: str, **fields) -> bool:
    allowed = {
        k: v for k, v in fields.items() if k in ("proxy_url", "disabled", "note") and v is not None
    }
    if not allowed:
        return False
    result = await collection.update_one({"_id": account_id}, {"$set": allowed})
    return result.matched_count > 0


async def delete(account_id: str) -> bool:
    result = await collection.delete_one({"_id": account_id})
    return result.deleted_count > 0


async def touch(account_id: str) -> None:
    await collection.update_one({"_id": account_id}, {"$set": {"last_used_at": _now()}})
