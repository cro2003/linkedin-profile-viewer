"""Inbound rate limiting, sliding window over a Redis sorted set.

Two separate budgets: writes (which can cost an upstream fetch and an account's
goodwill) are limited far more tightly than cached reads.
"""

import logging
import secrets
import time

from fastapi import HTTPException, Request

from app import metrics
from app.config import settings
from app.db import redis

log = logging.getLogger(__name__)


def client_identity(request: Request, api_key: str | None) -> str:
    """Prefer the API key; fall back to IP.

    X-Forwarded-For is only honoured when we are knowingly behind a proxy that
    sets it — otherwise any caller can forge it and dodge the limit entirely.
    """
    if api_key:
        return f"key:{api_key[:12]}"
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def check(identity: str, bucket: str, limit: int, window_sec: int) -> tuple[int, int]:
    """Returns (remaining, retry_after). Raises HTTPException(429) when over.

    ponytail: counts then adds, so a burst of concurrent requests can slip one or
    two over the limit. Move to a Lua script if exactness ever matters.
    """
    key = f"rl:{bucket}:{identity}"
    now = time.time()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now - window_sec)
            pipe.zcard(key)
            pipe.expire(key, window_sec + 1)
            _, used, _ = await pipe.execute()
    except Exception as e:
        log.warning("rate limiter unavailable, allowing request: %s", e)
        return limit, 0

    if used >= limit:
        oldest = await redis.zrange(key, 0, 0, withscores=True)
        retry_after = max(1, int(window_sec - (now - oldest[0][1]))) if oldest else window_sec
        await metrics.incr("rate_limited")
        raise HTTPException(429, {
            "code": "rate_limited",
            "message": f"{limit} requests per {window_sec}s exceeded",
            "retryable": True,
            "retry_after": retry_after,
        })

    await redis.zadd(key, {f"{now}:{secrets.token_hex(4)}": now})
    return limit - used - 1, 0
