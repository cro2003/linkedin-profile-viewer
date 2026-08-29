"""Runtime-editable configuration.

Environment values are the defaults; the admin panel writes overrides to Mongo.
Both the API and the worker re-apply them (the worker before each job), so a change
takes effect without a redeploy.
"""

import logging

from app.config import settings
from app.db import db

log = logging.getLogger(__name__)

collection = db.config
DOC_ID = "runtime"

# only these may be changed at runtime, with the coercion to apply
EDITABLE: dict[str, type] = {
    "rate_limit_write_per_min": int,
    "rate_limit_read_per_min": int,
    "account_min_delay_sec": float,
    "account_max_delay_sec": float,
    "request_timeout_sec": float,
    "cache_ttl_hours": int,
    "negative_cache_ttl_hours": int,
    "anon_free_lookups": int,
    "anon_ip_lookups": int,
    "proxy_required": bool,
}


def _coerce(name: str, value):
    kind = EDITABLE[name]
    if kind is bool:
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    coerced = kind(value)
    if coerced < 0:
        raise ValueError(f"{name} cannot be negative")
    return coerced


async def get_overrides() -> dict:
    doc = await collection.find_one({"_id": DOC_ID}) or {}
    return {k: v for k, v in doc.items() if k in EDITABLE}


async def set_overrides(values: dict) -> dict:
    clean = {}
    for name, value in values.items():
        if name not in EDITABLE:
            raise ValueError(f"{name} is not runtime-editable")
        clean[name] = _coerce(name, value)

    if "account_min_delay_sec" in clean or "account_max_delay_sec" in clean:
        current = await get_overrides()
        low = clean.get("account_min_delay_sec", current.get(
            "account_min_delay_sec", settings.account_min_delay_sec))
        high = clean.get("account_max_delay_sec", current.get(
            "account_max_delay_sec", settings.account_max_delay_sec))
        if low > high:
            raise ValueError("account_min_delay_sec cannot exceed account_max_delay_sec")

    await collection.update_one({"_id": DOC_ID}, {"$set": clean}, upsert=True)
    apply_values(clean)
    return clean


def apply_values(values: dict) -> None:
    for name, value in values.items():
        if name in EDITABLE:
            setattr(settings, name, value)


async def apply_stored() -> None:
    try:
        apply_values(await get_overrides())
    except Exception as e:
        log.warning("could not apply stored config: %s", e)


async def current() -> dict:
    """Effective values plus which of them are overridden."""
    overrides = await get_overrides()
    return {
        "effective": {name: getattr(settings, name) for name in EDITABLE},
        "overridden": sorted(overrides),
        "editable": sorted(EDITABLE),
    }
