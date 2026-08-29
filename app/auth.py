"""Users, sessions, API keys and the anonymous quota.

Password hashing uses hashlib.scrypt from the standard library rather than adding a
dependency. API keys are stored **hashed**: the plaintext is shown once, at creation,
so a database leak cannot hand anyone a working key.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import UTC, datetime

from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.config import settings
from app.db import db, redis

log = logging.getLogger(__name__)

users = db.users

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

# scrypt parameters: interactive-login cost, not batch-hashing cost
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1

ROLE_USER, ROLE_SUPERADMIN = "user", "superadmin"


def _now() -> datetime:
    return datetime.now(UTC)


# --- passwords ---


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    candidate, _ = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, password_hash)


# --- api keys ---


def new_api_key() -> tuple[str, str, str]:
    """Returns (plaintext, hash, prefix). Only the hash and prefix are stored."""
    key = f"lpg_{secrets.token_urlsafe(32)}"
    return key, hash_api_key(key), key[:12]


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# --- users ---


async def ensure_indexes() -> None:
    await users.create_index("email", unique=True)
    await users.create_index("api_key_hash")


def validate_credentials(email: str, password: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, {"code": "invalid_email", "message": "a valid email is required"})
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            422,
            {
                "code": "weak_password",
                "message": f"password must be at least {MIN_PASSWORD_LENGTH} characters",
            },
        )
    return email


async def create_user(email: str, password: str, role: str = ROLE_USER) -> tuple[dict, str]:
    email = validate_credentials(email, password)
    if await users.find_one({"email": email}):
        raise HTTPException(409, {"code": "email_taken", "message": "email already registered"})

    password_hash, salt = hash_password(password)
    key, key_hash, prefix = new_api_key()
    doc = {
        "_id": secrets.token_hex(12),
        "email": email,
        "password_hash": password_hash,
        "salt": salt,
        "role": role,
        "api_key_hash": key_hash,
        "api_key_prefix": prefix,
        "api_key_created_at": _now(),
        "disabled": False,
        "created_at": _now(),
        "lookups_used": 0,
    }
    await users.insert_one(doc)
    log.info("created %s account %s", role, email)
    return doc, key


async def authenticate(email: str, password: str) -> dict:
    user = await users.find_one({"email": (email or "").strip().lower()})
    if not user or not verify_password(password, user["password_hash"], user["salt"]):
        # same response either way, so the endpoint cannot be used to enumerate emails
        raise HTTPException(
            401, {"code": "invalid_credentials", "message": "email or password is incorrect"}
        )
    if user.get("disabled"):
        raise HTTPException(403, {"code": "account_disabled", "message": "account is disabled"})
    return user


async def regenerate_api_key(user_id: str) -> str:
    key, key_hash, prefix = new_api_key()
    await users.update_one(
        {"_id": user_id},
        {
            "$set": {
                "api_key_hash": key_hash,
                "api_key_prefix": prefix,
                "api_key_created_at": _now(),
            }
        },
    )
    return key


async def bootstrap_superadmin() -> None:
    if not (settings.superadmin_email and settings.superadmin_password):
        return
    email = settings.superadmin_email.strip().lower()
    if await users.find_one({"email": email}):
        return
    try:
        await create_user(email, settings.superadmin_password, role=ROLE_SUPERADMIN)
        log.info("bootstrapped superadmin %s", email)
    except HTTPException as e:
        log.warning("superadmin bootstrap skipped: %s", e.detail)


# --- sessions ---


async def start_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    await redis.set(f"sess:{token}", user_id, ex=settings.session_ttl_days * 86400)
    return token


async def end_session(token: str) -> None:
    await redis.delete(f"sess:{token}")


async def user_for_session(token: str) -> dict | None:
    user_id = await redis.get(f"sess:{token}")
    return await users.find_one({"_id": user_id}) if user_id else None


async def user_for_api_key(key: str) -> dict | None:
    return await users.find_one({"api_key_hash": hash_api_key(key)})


# --- callers ---


class Caller:
    """Who is making this request, and how it should be metered."""

    def __init__(self, kind: str, user: dict | None = None, identity: str = "anon"):
        self.kind = kind  # user | env_key | anon
        self.user = user
        self.identity = identity  # rate-limit bucket key

    @property
    def is_authenticated(self) -> bool:
        return self.kind in ("user", "env_key")

    @property
    def is_superadmin(self) -> bool:
        return bool(self.user and self.user.get("role") == ROLE_SUPERADMIN)


def client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def resolve_caller(
    request: Request,
    x_api_key: str | None = Header(default=None),
    session: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Caller:
    """Session first (the UI), then API key, then anonymous."""
    if session:
        user = await user_for_session(session)
        if user and not user.get("disabled"):
            return Caller("user", user, f"user:{user['_id']}")

    if x_api_key:
        user = await user_for_api_key(x_api_key)
        if user and not user.get("disabled"):
            return Caller("user", user, f"user:{user['_id']}")
        # keys from the environment stay valid: they predate user accounts and are
        # what the deployment's own tooling uses
        if x_api_key in settings.api_keys:
            return Caller("env_key", None, f"key:{x_api_key[:12]}")
        raise HTTPException(401, {"code": "unauthorized", "message": "invalid API key"})

    anon_id = getattr(request.state, "anon_id", None)
    return Caller("anon", None, f"anon:{anon_id or client_ip(request)}")


async def require_user(caller: Caller = Depends(resolve_caller)) -> Caller:
    if not caller.is_authenticated:
        raise HTTPException(401, {"code": "login_required", "message": "sign in to continue"})
    return caller


async def require_superadmin(caller: Caller = Depends(resolve_caller)) -> Caller:
    if caller.kind == "env_key":
        return caller  # deployment tooling
    if not caller.is_superadmin:
        raise HTTPException(403, {"code": "forbidden", "message": "superadmin only"})
    return caller


# --- anonymous quota ---


async def anon_usage(anon_id: str, ip: str) -> tuple[int, int]:
    used_browser, used_ip = await redis.mget([f"anon:{anon_id}", f"anonip:{ip}"])
    return int(used_browser or 0), int(used_ip or 0)


async def check_anon_quota(anon_id: str, ip: str) -> int:
    """Returns remaining free lookups, or raises once the quota is spent."""
    used_browser, used_ip = await anon_usage(anon_id, ip)
    if used_browser >= settings.anon_free_lookups or used_ip >= settings.anon_ip_lookups:
        raise HTTPException(
            402,
            {
                "code": "signup_required",
                "message": f"free limit of {settings.anon_free_lookups} lookups reached, "
                f"create an account to continue",
                "retryable": False,
            },
        )
    return settings.anon_free_lookups - used_browser


async def consume_anon_quota(anon_id: str, ip: str) -> None:
    ttl = 90 * 86400
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(f"anon:{anon_id}")
        pipe.expire(f"anon:{anon_id}", ttl)
        pipe.incr(f"anonip:{ip}")
        pipe.expire(f"anonip:{ip}", ttl)
        await pipe.execute()


async def record_user_lookup(user_id: str) -> None:
    await users.update_one({"_id": user_id}, {"$inc": {"lookups_used": 1}})
