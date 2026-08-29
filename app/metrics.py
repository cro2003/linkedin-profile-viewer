"""Counters in Redis. Cheap enough to be worth having, small enough not to need
a metrics stack — /v1/stats reads them straight back."""

from app.db import redis

PREFIX = "stat:"
TRACKED = (
    "requests",
    "cache_hits",
    "negative_hits",
    "jobs_queued",
    "fetches",
    "retries",
    "failures",
    "session_refreshes",
    "checkpoints",
    "rate_limited",
)


async def incr(name: str, amount: int = 1) -> None:
    try:
        await redis.incrby(f"{PREFIX}{name}", amount)
    except Exception:
        pass  # metrics must never break a request


async def snapshot() -> dict[str, int]:
    values = await redis.mget([f"{PREFIX}{n}" for n in TRACKED])
    return {name: int(v or 0) for name, v in zip(TRACKED, values, strict=False)}
