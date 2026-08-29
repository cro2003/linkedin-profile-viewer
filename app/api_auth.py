"""Signup, login, session and API-key endpoints."""

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app import auth
from app.config import settings

router = APIRouter(prefix="/v1", tags=["auth"])

COOKIE_KWARGS = dict(httponly=True, samesite="lax", path="/")


class Credentials(BaseModel):
    email: str
    password: str = Field(..., min_length=1)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(settings.session_cookie_name, token,
                        max_age=settings.session_ttl_days * 86400,
                        secure=settings.cookie_secure, **COOKIE_KWARGS)


def _user_view(user: dict) -> dict:
    return {
        "id": user["_id"],
        "email": user["email"],
        "role": user["role"],
        "api_key_prefix": user.get("api_key_prefix"),
        "lookups_used": user.get("lookups_used", 0),
        "created_at": user["created_at"].isoformat() if user.get("created_at") else None,
    }


@router.post("/auth/signup")
async def signup(body: Credentials, response: Response):
    """Creates the account and returns the API key — the only time it is shown."""
    user, api_key = await auth.create_user(body.email, body.password)
    _set_session_cookie(response, await auth.start_session(user["_id"]))
    return {"user": _user_view(user), "api_key": api_key,
            "notice": "store this key now, it is not shown again"}


@router.post("/auth/login")
async def login(body: Credentials, response: Response):
    user = await auth.authenticate(body.email, body.password)
    _set_session_cookie(response, await auth.start_session(user["_id"]))
    return {"user": _user_view(user)}


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await auth.end_session(token)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
async def me(caller: auth.Caller = Depends(auth.require_user)):
    if caller.kind == "env_key":
        return {"user": None, "kind": "env_key"}
    return {"user": _user_view(caller.user), "kind": "user"}


@router.post("/me/api-key")
async def rotate_api_key(caller: auth.Caller = Depends(auth.require_user)):
    if caller.kind == "env_key":
        return {"error": {"code": "not_applicable",
                          "message": "environment keys are not rotatable here"}}
    key = await auth.regenerate_api_key(caller.user["_id"])
    return {"api_key": key, "notice": "store this key now, it is not shown again"}


@router.get("/quota")
async def quota(request: Request, caller: auth.Caller = Depends(auth.resolve_caller)):
    """Drives the 'N free lookups left' banner."""
    if caller.is_authenticated:
        return {"authenticated": True, "limit_per_min": settings.rate_limit_write_per_min,
                "lookups_used": (caller.user or {}).get("lookups_used", 0)}
    anon_id = getattr(request.state, "anon_id", "unknown")
    used_browser, _ = await auth.anon_usage(anon_id, auth.client_ip(request))
    return {"authenticated": False,
            "free_total": settings.anon_free_lookups,
            "free_used": min(used_browser, settings.anon_free_lookups),
            "free_remaining": max(0, settings.anon_free_lookups - used_browser)}
