"""Admin endpoints. Superadmin only (environment API keys also pass, for tooling)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import accounts, auth, logins, metrics, pool, runtime
from app.db import redis
from app.worker import job_id_for  # noqa: F401  (kept for symmetry with the lookup API

router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(auth.require_superadmin)]
)


class AccountIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    mode: str = Field("cookies", pattern="^(cookies|login)$")
    cookies: dict[str, str] | None = None
    email: str | None = None
    password: str | None = None
    proxy_url: str | None = None
    note: str | None = None


class AccountPatch(BaseModel):
    disabled: bool | None = None
    proxy_url: str | None = None
    note: str | None = None
    clear_cooldown: bool | None = None
    reset_status: bool | None = None


class OtpIn(BaseModel):
    code: str = Field(..., min_length=3, max_length=12)


class UserPatch(BaseModel):
    disabled: bool | None = None
    role: str | None = Field(None, pattern="^(user|superadmin)$")


@router.get("/accounts")
async def list_accounts():
    return {"accounts": await pool.snapshot()}


@router.post("/accounts")
async def add_account(body: AccountIn, request: Request):
    """Two ways in: paste a harvested cookie jar, or drive a real login (which may
    need a verification code, relayed through /logins/{id}/otp)."""
    if body.mode == "cookies":
        if not body.cookies:
            raise HTTPException(
                422, {"code": "cookies_required", "message": "cookies are required in cookies mode"}
            )
        try:
            await accounts.create(
                body.id,
                body.cookies,
                proxy_url=body.proxy_url,
                email=body.email,
                note=body.note or "added via admin",
            )
        except ValueError as e:
            raise HTTPException(422, {"code": "invalid_cookies", "message": str(e)}) from e
        return {"account_id": body.id, "status": "created"}

    if not (body.email and body.password):
        raise HTTPException(
            422,
            {
                "code": "credentials_required",
                "message": "email and password are required in login mode",
            },
        )
    if not request.app.state.arq:
        raise HTTPException(
            503, {"code": "queue_unavailable", "message": "job queue is not reachable"}
        )

    login_id = logins.new_id()
    await logins.set_status(login_id, logins.RUNNING, account_id=body.id, step="starting")
    await request.app.state.arq.enqueue_job(
        "add_account_job", login_id, body.id, body.email, body.password, body.proxy_url, body.note
    )
    # the password is passed to the job and never stored
    return {
        "login_id": login_id,
        "account_id": body.id,
        "status": logins.RUNNING,
        "poll_url": f"/v1/admin/logins/{login_id}",
    }


@router.get("/logins/{login_id}")
async def login_status(login_id: str):
    status = await logins.get_status(login_id)
    if not status:
        raise HTTPException(
            404, {"code": "login_not_found", "message": "unknown or expired login attempt"}
        )
    return status


@router.post("/logins/{login_id}/otp")
async def submit_otp(login_id: str, body: OtpIn):
    if not await logins.submit_otp(login_id, body.code):
        raise HTTPException(
            404, {"code": "login_not_found", "message": "unknown or expired login attempt"}
        )
    return {"ok": True}


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: str, body: AccountPatch):
    if not await accounts.get_account(account_id):
        raise HTTPException(404, {"code": "account_not_found", "message": account_id})

    await accounts.update(
        account_id, disabled=body.disabled, proxy_url=body.proxy_url, note=body.note
    )
    if body.clear_cooldown:
        await redis.delete(f"acct:{account_id}:next_ok_at")
    if body.reset_status:
        await pool.set_status(account_id, pool.LIVE)
    return {"account_id": account_id, "status": "updated"}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    if not await accounts.delete(account_id):
        raise HTTPException(404, {"code": "account_not_found", "message": account_id})
    for suffix in ("lock", "next_ok_at", "status"):
        await redis.delete(f"acct:{account_id}:{suffix}")
    return {"account_id": account_id, "status": "deleted"}


@router.get("/config")
async def get_config():
    return await runtime.current()


@router.patch("/config")
async def patch_config(values: dict):
    try:
        applied = await runtime.set_overrides(values)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, {"code": "invalid_config", "message": str(e)}) from e
    return {"applied": applied, "config": await runtime.current()}


@router.get("/users")
async def list_users():
    out = []
    async for user in auth.users.find({}).sort("created_at", -1).limit(200):
        out.append(
            {
                "id": user["_id"],
                "email": user["email"],
                "role": user["role"],
                "disabled": bool(user.get("disabled")),
                "lookups_used": user.get("lookups_used", 0),
                "api_key_prefix": user.get("api_key_prefix"),
                "created_at": user["created_at"].isoformat() if user.get("created_at") else None,
            }
        )
    return {"users": out}


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str, body: UserPatch, caller: auth.Caller = Depends(auth.require_superadmin)
):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(422, {"code": "nothing_to_update", "message": "no fields given"})
    # refuse to let the signed-in admin lock themselves out
    if (
        caller.user
        and caller.user["_id"] == user_id
        and (updates.get("disabled") or updates.get("role") == auth.ROLE_USER)
    ):
        raise HTTPException(
            422, {"code": "self_lockout", "message": "cannot disable or demote your own account"}
        )
    result = await auth.users.update_one({"_id": user_id}, {"$set": updates})
    if not result.matched_count:
        raise HTTPException(404, {"code": "user_not_found", "message": user_id})
    return {"user_id": user_id, "updated": updates}


@router.get("/stats")
async def stats():
    return {"counters": await metrics.snapshot(), "accounts": await pool.snapshot()}
