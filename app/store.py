"""Mongo-backed profile cache and job records.

The cache is the single most useful robustness feature here: repeat requests never
touch LinkedIn, and a previously-fetched profile keeps serving even while the
upstream session is broken. The raw payload is kept alongside the parsed doc so a
parser fix can be replayed without re-scraping.
"""

from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from app.config import settings
from app.db import jobs, profiles
from app.models import SCHEMA_VERSION, Profile


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # pymongo returns naive datetimes; everything stored here is UTC
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def ensure_indexes() -> None:
    await profiles.create_index("fetched_at")
    await jobs.create_index("created_at")


def is_fresh(doc: dict) -> bool:
    hours = settings.negative_cache_ttl_hours if doc.get("error") else settings.cache_ttl_hours
    return _as_utc(doc["fetched_at"]) > _now() - timedelta(hours=hours)


async def get_cached(public_id: str) -> dict | None:
    """Returns the stored doc only while it is inside its TTL."""
    doc = await profiles.find_one({"_id": public_id})
    if not doc or not is_fresh(doc):
        return None
    return doc


async def get_any(public_id: str) -> dict | None:
    """Stored doc regardless of age — used by the cache-only read endpoint."""
    return await profiles.find_one({"_id": public_id})


async def save_profile(profile: Profile, raw: dict, sections: dict,
                       partial: list[str], account_id: str) -> dict:
    doc = {
        "_id": profile.public_id,
        "profile": profile.model_dump(mode="json"),
        "raw": raw,
        "fetched_at": _now(),
        "source": "api",
        "schema_version": SCHEMA_VERSION,
        "sections": {k: (v.model_dump() if hasattr(v, "model_dump") else v)
                     for k, v in sections.items()},
        "partial_sections": partial,
        "account_id": account_id,
        "error": None,
    }
    await profiles.replace_one({"_id": profile.public_id}, doc, upsert=True)
    return doc


async def save_negative(public_id: str, code: str, message: str) -> None:
    """Cache a definitive failure briefly so repeats do not burn an account."""
    await profiles.replace_one(
        {"_id": public_id},
        {"_id": public_id, "profile": None, "raw": None, "fetched_at": _now(),
         "error": {"code": code, "message": message}},
        upsert=True,
    )


async def create_job(job_id: str, public_id: str) -> dict:
    """Insert-only upsert: job ids are stable per profile, so a request arriving
    while an earlier one is still running must not reset its state."""
    await jobs.update_one(
        {"_id": job_id},
        {"$setOnInsert": {"public_id": public_id, "status": "queued",
                          "created_at": _now(), "updated_at": _now(),
                          "events": [{"status": "queued", "at": _now()}],
                          "attempts": 0, "error": None}},
        upsert=True,
    )
    return await jobs.find_one({"_id": job_id})


async def update_job(job_id: str, status: str, **fields) -> dict | None:
    """Upsert so a worker that starts before the API's insert still records progress."""
    return await jobs.find_one_and_update(
        {"_id": job_id},
        {"$set": {"status": status, "updated_at": _now(), **fields},
         "$push": {"events": {"status": status, "at": _now()}},
         "$setOnInsert": {"created_at": _now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def get_job(job_id: str) -> dict | None:
    return await jobs.find_one({"_id": job_id})
