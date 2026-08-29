"""State for an in-progress account login.

The browser has to stay open between submitting credentials and receiving the
verification code, so it lives inside a single worker job. This module is just the
shared mailbox between that job and the admin panel.
"""

import json
import secrets

from app.db import redis

TTL = 900  # 15 minutes

RUNNING, AWAITING_OTP, DONE, FAILED = "running", "awaiting_otp", "done", "failed"


def new_id() -> str:
    return secrets.token_urlsafe(12)


def _status_key(login_id: str) -> str:
    return f"login:{login_id}:status"


def _otp_key(login_id: str) -> str:
    return f"login:{login_id}:otp"


async def set_status(login_id: str, status: str, **fields) -> None:
    await redis.set(_status_key(login_id), json.dumps({"status": status, **fields}), ex=TTL)


async def get_status(login_id: str) -> dict | None:
    raw = await redis.get(_status_key(login_id))
    return json.loads(raw) if raw else None


async def submit_otp(login_id: str, code: str) -> bool:
    if not await redis.exists(_status_key(login_id)):
        return False
    await redis.set(_otp_key(login_id), code.strip(), ex=TTL)
    return True


async def take_otp(login_id: str) -> str | None:
    """Reads and clears the code, so a stale one is never reused."""
    code = await redis.get(_otp_key(login_id))
    if code:
        await redis.delete(_otp_key(login_id))
    return code
