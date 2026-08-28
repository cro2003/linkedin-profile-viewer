import logging
from contextlib import asynccontextmanager
from datetime import timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import store
from app.config import settings
from app.db import mongo, redis
from app.linkedin.client import FetchError, PermanentError, ProfileClient
from app.linkedin.parse import ProfileNotInPayload, parse_profile
from app.models import Meta, Profile, ProfileResponse
from app.urls import InvalidProfileURL, public_id_from_url

logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await store.ensure_indexes()
    except Exception as e:  # a cold Mongo should not stop the app from booting
        log.warning("index creation skipped: %s", e)
    yield


app = FastAPI(title="LinkedIn Profile API", version="0.1.0", lifespan=lifespan)


def error_response(status: int, code: str, message: str, retryable: bool = False):
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message,
                                           "retryable": retryable}})


@app.exception_handler(HTTPException)
async def http_error(request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "message": str(exc.detail)}
    return error_response(exc.status_code, detail.get("code", "error"),
                          detail.get("message", ""), detail.get("retryable", False))


async def require_api_key(x_api_key: str | None = Header(default=None)):
    # no keys configured = open, for local development only
    if not settings.api_keys:
        return
    if x_api_key not in settings.api_keys:
        raise HTTPException(401, {"code": "unauthorized", "message": "valid X-API-Key required"})


class ProfileRequest(BaseModel):
    url: str = Field(..., description="LinkedIn profile URL or vanity slug")
    refresh: bool = Field(False, description="bypass the cache and refetch")


def _response_from_doc(doc: dict, cache_hit: bool) -> ProfileResponse:
    return ProfileResponse(
        data=Profile.model_validate(doc["profile"]),
        meta=Meta(
            fetched_at=doc["fetched_at"].replace(tzinfo=timezone.utc)
            if doc["fetched_at"].tzinfo is None else doc["fetched_at"],
            cache_hit=cache_hit,
            source=doc.get("source", "api"),
            unavailable_sections=doc.get("unavailable_sections", []),
            partial_sections=doc.get("partial_sections", []),
        ),
    )


async def _fetch_and_store(public_id: str) -> dict:
    """Synchronous fetch path. Phase 4 replaces this with an enqueue."""
    if not settings.linkedin_accounts:
        raise HTTPException(503, {"code": "no_accounts",
                                  "message": "no LinkedIn accounts configured",
                                  "retryable": False})
    account = settings.linkedin_accounts[0]
    async with ProfileClient(account) as client:
        payload = await client.fetch_profile(public_id)
    profile, unavailable, partial = parse_profile(payload, public_id)
    return await store.save_profile(profile, payload, unavailable, partial, account.id)


@app.post("/v1/profiles", response_model=ProfileResponse,
          dependencies=[Depends(require_api_key)])
async def create_profile_request(body: ProfileRequest):
    try:
        public_id = public_id_from_url(body.url)
    except InvalidProfileURL as e:
        raise HTTPException(422, {"code": "invalid_url", "message": str(e)})

    if not body.refresh:
        cached = await store.get_cached(public_id)
        if cached and cached.get("error"):
            err = cached["error"]
            raise HTTPException(404, {"code": err["code"], "message": err["message"]})
        if cached:
            return _response_from_doc(cached, cache_hit=True)

    try:
        doc = await _fetch_and_store(public_id)
    except (PermanentError, ProfileNotInPayload) as e:
        await store.save_negative(public_id, "profile_not_found", str(e))
        raise HTTPException(404, {"code": "profile_not_found", "message": str(e)})
    except FetchError as e:
        # transient or session problem: stale cache beats an error
        stale = await store.get_any(public_id)
        if stale and stale.get("profile"):
            log.warning("serving stale profile for %s after %s", public_id, type(e).__name__)
            return _response_from_doc(stale, cache_hit=True)
        raise HTTPException(503, {"code": "upstream_unavailable", "message": str(e),
                                  "retryable": True})
    return _response_from_doc(doc, cache_hit=False)


@app.get("/v1/profiles/{public_id}", response_model=ProfileResponse,
         dependencies=[Depends(require_api_key)])
async def read_cached_profile(public_id: str,
                              allow_stale: bool = Query(True, description="serve past TTL")):
    doc = await store.get_any(public_id) if allow_stale else await store.get_cached(public_id)
    if not doc or not doc.get("profile"):
        raise HTTPException(404, {"code": "not_cached",
                                  "message": f"no cached profile for {public_id}"})
    return _response_from_doc(doc, cache_hit=True)


@app.get("/health")
async def health():
    deps = {}
    for name, ping in (("mongo", mongo.admin.command("ping")), ("redis", redis.ping())):
        try:
            await ping
            deps[name] = "ok"
        except Exception as e:
            deps[name] = f"error: {type(e).__name__}"
    ok = all(v == "ok" for v in deps.values())
    return {"status": "ok" if ok else "degraded", "deps": deps,
            "accounts": len(settings.linkedin_accounts)}
