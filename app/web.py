"""Server-rendered pages. Kept deliberately thin: every page talks to the same
JSON API the public uses, so there is no second code path to keep in sync."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.config import settings

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


async def session_user(request: Request) -> dict | None:
    """Page-level auth: session cookie only. Never raises — pages redirect instead."""
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    user = await auth.user_for_session(token)
    return None if (user and user.get("disabled")) else user


def _page(request: Request, name: str, user: dict | None, **context):
    return templates.TemplateResponse(request, name, {"user": user, **context})


@router.get("/")
async def index(request: Request):
    return _page(request, "index.html", await session_user(request))


@router.get("/login")
async def login_page(request: Request):
    if await session_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return _page(request, "auth.html", None, mode="login")


@router.get("/signup")
async def signup_page(request: Request):
    if await session_user(request):
        return RedirectResponse("/dashboard", status_code=302)
    return _page(request, "auth.html", None, mode="signup")


@router.get("/dashboard")
async def dashboard(request: Request):
    user = await session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "dashboard.html", user)


@router.get("/admin")
async def admin(request: Request):
    user = await session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.get("role") != auth.ROLE_SUPERADMIN:
        return RedirectResponse("/dashboard", status_code=302)
    return _page(request, "admin.html", user)
