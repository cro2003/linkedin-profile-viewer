"""FastAPI application: profile lookup, job status and progress streaming.

A lookup is served from cache when it can be and queued otherwise, so the HTTP
layer never blocks on an upstream fetch. Auth, admin and the web pages live in
api_auth.py, api_admin.py and web.py.
"""

import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import accounts, api_admin, api_auth, auth, metrics, pool, ratelimit, runtime, store, web
from app.config import settings
from app.db import mongo, redis
from app.models import Meta, Profile, ProfileResponse
from app.urls import InvalidProfileURL, public_id_from_url
from app.worker import job_id_for

logging.basicConfig(
    level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger(__name__)

TERMINAL = ("done", "failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq = None
    try:
        app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await accounts.seed_from_env()
        await accounts.ensure_indexes()
        await auth.ensure_indexes()
        await auth.bootstrap_superadmin()
        await store.ensure_indexes()
        await runtime.apply_stored()
    except Exception as e:  # boot even with cold dependencies; /health reports it
        log.warning("startup partially failed: %s", e)
    yield
    if app.state.arq:
        await app.state.arq.close()


app = FastAPI(title="Sourcely API", version="0.3.0", lifespan=lifespan)
app.include_router(api_auth.router)
app.include_router(api_admin.router)
app.include_router(web.router)


@app.middleware("http")
async def anonymous_identity(request: Request, call_next):
    """Give every unauthenticated browser a stable id, so the free-lookup quota is
    not merely per-IP. Resolved before the route runs so the first request counts."""
    existing = request.cookies.get(settings.anon_cookie_name)
    request.state.anon_id = existing or secrets.token_urlsafe(16)
    response = await call_next(request)
    if not existing:
        response.set_cookie(
            settings.anon_cookie_name,
            request.state.anon_id,
            max_age=90 * 86400,
            httponly=True,
            samesite="lax",
            path="/",
            secure=settings.cookie_secure,
        )
    return response


def error_response(
    status: int, code: str, message: str, retryable: bool = False, retry_after: int | None = None
):
    body = {"code": code, "message": message, "retryable": retryable}
    if retry_after:
        body["retry_after"] = retry_after
    return JSONResponse(status_code=status, content={"error": body})


@app.exception_handler(HTTPException)
async def http_error(request, exc: HTTPException):
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"code": "error", "message": str(exc.detail)}
    )
    retry_after = detail.get("retry_after")
    response = error_response(
        exc.status_code,
        detail.get("code", "error"),
        detail.get("message", ""),
        detail.get("retryable", False),
        retry_after,
    )
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


async def require_api_key(x_api_key: str | None = Header(default=None)):
    # no keys configured = open, for local development only
    if not settings.api_keys:
        return
    if x_api_key not in settings.api_keys:
        raise HTTPException(401, {"code": "unauthorized", "message": "valid X-API-Key required"})


async def limit_writes(caller: auth.Caller = Depends(auth.resolve_caller)):
    await ratelimit.check(caller.identity, "write", settings.rate_limit_write_per_min, 60)


async def limit_reads(caller: auth.Caller = Depends(auth.resolve_caller)):
    await ratelimit.check(caller.identity, "read", settings.rate_limit_read_per_min, 60)


class ProfileRequest(BaseModel):
    url: str = Field(..., description="LinkedIn profile URL or vanity slug")
    refresh: bool = Field(False, description="bypass the cache and refetch")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _response_from_doc(doc: dict, cache_hit: bool) -> ProfileResponse:
    return ProfileResponse(
        data=Profile.model_validate(doc["profile"]),
        meta=Meta(
            fetched_at=_utc(doc["fetched_at"]),
            cache_hit=cache_hit,
            source=doc.get("source", "api"),
            sections=doc.get("sections", {}),
            partial_sections=doc.get("partial_sections", []),
        ),
    )


def _job_view(doc: dict) -> dict:
    return {
        "job_id": doc["_id"],
        "public_id": doc["public_id"],
        "status": doc["status"],
        "attempts": doc.get("attempts", 0),
        "error": doc.get("error"),
        "created_at": _utc(doc["created_at"]).isoformat(),
        "updated_at": _utc(doc["updated_at"]).isoformat(),
        "events": [
            {"status": e["status"], "at": _utc(e["at"]).isoformat()} for e in doc.get("events", [])
        ],
        "result_url": (f"/v1/profiles/{doc['public_id']}" if doc["status"] == "done" else None),
    }


@app.post("/v1/profiles", dependencies=[Depends(limit_writes)])
async def create_profile_request(
    body: ProfileRequest, request: Request, caller: auth.Caller = Depends(auth.resolve_caller)
):
    """Cache hit returns the profile directly; a miss queues a job and returns 202."""
    try:
        public_id = public_id_from_url(body.url)
    except InvalidProfileURL as e:
        raise HTTPException(422, {"code": "invalid_url", "message": str(e)}) from e

    await metrics.incr("requests")

    anon_id = getattr(request.state, "anon_id", "unknown")
    ip = auth.client_ip(request)
    if not caller.is_authenticated:
        await auth.check_anon_quota(anon_id, ip)

    async def charge():
        """A lookup is a lookup whether or not the cache served it."""
        if caller.is_authenticated:
            if caller.user:
                await auth.record_user_lookup(caller.user["_id"])
        else:
            await auth.consume_anon_quota(anon_id, ip)

    if not body.refresh:
        cached = await store.get_cached(public_id)
        if cached and cached.get("error"):
            await metrics.incr("negative_hits")
            err = cached["error"]
            raise HTTPException(404, {"code": err["code"], "message": err["message"]})
        if cached:
            await metrics.incr("cache_hits")
            await charge()
            return _response_from_doc(cached, cache_hit=True).model_dump(mode="json")

    # accounts live in Mongo; the environment is only ever a first-boot seed
    if not await accounts.list_accounts():
        raise HTTPException(
            503,
            {
                "code": "no_accounts",
                "message": "no LinkedIn accounts configured; add one in /admin",
                "retryable": False,
            },
        )
    if not request.app.state.arq:
        raise HTTPException(
            503,
            {
                "code": "queue_unavailable",
                "message": "job queue is not reachable",
                "retryable": True,
            },
        )

    job_id = job_id_for(public_id)
    # Enqueue first: a None return means this job id is already in flight, so
    # report that job's real state rather than claiming a fresh "queued".
    enqueued = await request.app.state.arq.enqueue_job(
        "fetch_profile_job", public_id, body.refresh, _job_id=job_id
    )

    if enqueued is None:
        existing = await store.get_job(job_id)
        status = existing["status"] if existing else "in_progress"
    else:
        await store.create_job(job_id, public_id)
        await metrics.incr("jobs_queued")
        status = "queued"

    await charge()
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "public_id": public_id,
            "status": status,
            "poll_url": f"/v1/jobs/{job_id}",
            "events_url": f"/v1/jobs/{job_id}/events",
        },
    )


@app.get("/v1/jobs/{job_id:path}/events", dependencies=[Depends(limit_reads)])
async def stream_job_events(job_id: str, request: Request):
    """Server-sent events for one job.

    Stored events replay first so a subscriber that connects late still sees the
    whole history, then live updates arrive over pubsub. Polling /v1/jobs/{id}
    remains available for clients that cannot hold a stream open.
    """
    doc = await store.get_job(job_id)
    if not doc:
        raise HTTPException(404, {"code": "job_not_found", "message": f"no job {job_id}"})

    async def stream():
        replay = await store.get_job(job_id)
        for event in (replay or {}).get("events", []):
            yield _sse(
                {"status": event["status"], "at": _utc(event["at"]).isoformat(), "replay": True}
            )
        if replay and replay["status"] in TERMINAL:
            yield _sse({"status": replay["status"], "final": True})
            return

        pubsub = redis.pubsub()
        await pubsub.subscribe(f"job:{job_id}")
        started = last_beat = time.time()
        try:
            while True:
                if await request.is_disconnected():
                    return
                if time.time() - started > settings.sse_max_duration_sec:
                    yield _sse({"status": "stream_timeout", "final": True})
                    return
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    payload = json.loads(message["data"])
                    yield _sse(payload)
                    if payload.get("status") in TERMINAL:
                        yield _sse({"status": payload["status"], "final": True})
                        return
                elif time.time() - last_beat > 15:
                    # keeps idle proxies from closing the connection
                    yield ": heartbeat\n\n"
                    last_beat = time.time()
        finally:
            await pubsub.unsubscribe(f"job:{job_id}")
            await pubsub.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stop proxies buffering the stream
        },
    )


@app.get("/v1/jobs/{job_id:path}", dependencies=[Depends(limit_reads)])
async def read_job(job_id: str):
    doc = await store.get_job(job_id)
    if not doc:
        raise HTTPException(404, {"code": "job_not_found", "message": f"no job {job_id}"})
    return _job_view(doc)


@app.get(
    "/v1/profiles/{public_id}", response_model=ProfileResponse, dependencies=[Depends(limit_reads)]
)
async def read_cached_profile(
    public_id: str, allow_stale: bool = Query(True, description="serve past TTL")
):
    doc = await store.get_any(public_id) if allow_stale else await store.get_cached(public_id)
    if not doc or not doc.get("profile"):
        raise HTTPException(
            404, {"code": "not_cached", "message": f"no cached profile for {public_id}"}
        )
    return _response_from_doc(doc, cache_hit=True)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/v1/stats", dependencies=[Depends(auth.require_superadmin)])
async def read_stats():
    return {"counters": await metrics.snapshot(), "accounts": await pool.snapshot()}


@app.get("/health")
async def health():
    deps = {}
    for name, ping in (("mongo", mongo.admin.command("ping")), ("redis", redis.ping())):
        try:
            await ping
            deps[name] = "ok"
        except Exception as e:
            deps[name] = f"error: {type(e).__name__}"
    try:
        accounts = await pool.snapshot()
    except Exception as e:
        accounts = [{"error": type(e).__name__}]
    ok = all(v == "ok" for v in deps.values())
    return {"status": "ok" if ok else "degraded", "deps": deps, "accounts": accounts}
