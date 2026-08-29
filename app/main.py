import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import accounts, metrics, pool, ratelimit, store
from app.config import settings
from app.db import mongo, redis
from app.models import Meta, Profile, ProfileResponse
from app.urls import InvalidProfileURL, public_id_from_url
from app.worker import fetch_profile_job, job_id_for

logging.basicConfig(level=settings.log_level,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

TERMINAL = ("done", "failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq = None
    try:
        app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await accounts.seed_from_env()
        await accounts.ensure_indexes()
        await store.ensure_indexes()
    except Exception as e:  # boot even with cold dependencies; /health reports it
        log.warning("startup partially failed: %s", e)
    yield
    if app.state.arq:
        await app.state.arq.close()


app = FastAPI(title="LinkedIn Profile API", version="0.2.0", lifespan=lifespan)


def error_response(status: int, code: str, message: str, retryable: bool = False,
                   retry_after: int | None = None):
    body = {"code": code, "message": message, "retryable": retryable}
    if retry_after:
        body["retry_after"] = retry_after
    return JSONResponse(status_code=status, content={"error": body})


@app.exception_handler(HTTPException)
async def http_error(request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error",
                                                              "message": str(exc.detail)}
    retry_after = detail.get("retry_after")
    response = error_response(exc.status_code, detail.get("code", "error"),
                              detail.get("message", ""), detail.get("retryable", False),
                              retry_after)
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


async def require_api_key(x_api_key: str | None = Header(default=None)):
    # no keys configured = open, for local development only
    if not settings.api_keys:
        return
    if x_api_key not in settings.api_keys:
        raise HTTPException(401, {"code": "unauthorized", "message": "valid X-API-Key required"})


async def limit_writes(request: Request, x_api_key: str | None = Header(default=None)):
    identity = ratelimit.client_identity(request, x_api_key)
    await ratelimit.check(identity, "write", settings.rate_limit_write_per_min, 60)


async def limit_reads(request: Request, x_api_key: str | None = Header(default=None)):
    identity = ratelimit.client_identity(request, x_api_key)
    await ratelimit.check(identity, "read", settings.rate_limit_read_per_min, 60)


class ProfileRequest(BaseModel):
    url: str = Field(..., description="LinkedIn profile URL or vanity slug")
    refresh: bool = Field(False, description="bypass the cache and refetch")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _response_from_doc(doc: dict, cache_hit: bool) -> ProfileResponse:
    return ProfileResponse(
        data=Profile.model_validate(doc["profile"]),
        meta=Meta(
            fetched_at=_utc(doc["fetched_at"]),
            cache_hit=cache_hit,
            source=doc.get("source", "api"),
            unavailable_sections=doc.get("unavailable_sections", []),
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
        "events": [{"status": e["status"], "at": _utc(e["at"]).isoformat()}
                   for e in doc.get("events", [])],
        "result_url": (f"/v1/profiles/{doc['public_id']}"
                       if doc["status"] == "done" else None),
    }


@app.post("/v1/profiles",
          dependencies=[Depends(require_api_key), Depends(limit_writes)])
async def create_profile_request(body: ProfileRequest, request: Request):
    """Cache hit returns the profile directly; a miss queues a job and returns 202."""
    try:
        public_id = public_id_from_url(body.url)
    except InvalidProfileURL as e:
        raise HTTPException(422, {"code": "invalid_url", "message": str(e)})

    await metrics.incr("requests")

    if not body.refresh:
        cached = await store.get_cached(public_id)
        if cached and cached.get("error"):
            await metrics.incr("negative_hits")
            err = cached["error"]
            raise HTTPException(404, {"code": err["code"], "message": err["message"]})
        if cached:
            await metrics.incr("cache_hits")
            return _response_from_doc(cached, cache_hit=True).model_dump(mode="json")

    if not settings.linkedin_accounts:
        raise HTTPException(503, {"code": "no_accounts",
                                  "message": "no LinkedIn accounts configured"})
    if not request.app.state.arq:
        raise HTTPException(503, {"code": "queue_unavailable",
                                  "message": "job queue is not reachable", "retryable": True})

    job_id = job_id_for(public_id)
    # Enqueue first. A None return means arq already knows this job id, so a fetch
    # is in flight (or just finished) and duplicate requests share it — but we must
    # then report that job's real state rather than claiming a fresh "queued".
    enqueued = await request.app.state.arq.enqueue_job(
        "fetch_profile_job", public_id, body.refresh, _job_id=job_id)

    if enqueued is None:
        existing = await store.get_job(job_id)
        status = existing["status"] if existing else "in_progress"
    else:
        await store.create_job(job_id, public_id)
        await metrics.incr("jobs_queued")
        status = "queued"

    return JSONResponse(status_code=202, content={
        "job_id": job_id,
        "public_id": public_id,
        "status": status,
        "poll_url": f"/v1/jobs/{job_id}",
        "events_url": f"/v1/jobs/{job_id}/events",
    })


@app.get("/v1/jobs/{job_id:path}/events",
         dependencies=[Depends(require_api_key), Depends(limit_reads)])
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
        seen = 0
        replay = await store.get_job(job_id)
        for event in (replay or {}).get("events", []):
            seen += 1
            yield _sse({"status": event["status"], "at": _utc(event["at"]).isoformat(),
                        "replay": True})
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

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # stops proxies buffering the stream
    })


@app.get("/v1/jobs/{job_id:path}",
         dependencies=[Depends(require_api_key), Depends(limit_reads)])
async def read_job(job_id: str):
    doc = await store.get_job(job_id)
    if not doc:
        raise HTTPException(404, {"code": "job_not_found", "message": f"no job {job_id}"})
    return _job_view(doc)


@app.get("/v1/profiles/{public_id}", response_model=ProfileResponse,
         dependencies=[Depends(require_api_key), Depends(limit_reads)])
async def read_cached_profile(public_id: str,
                              allow_stale: bool = Query(True, description="serve past TTL")):
    doc = await store.get_any(public_id) if allow_stale else await store.get_cached(public_id)
    if not doc or not doc.get("profile"):
        raise HTTPException(404, {"code": "not_cached",
                                  "message": f"no cached profile for {public_id}"})
    return _response_from_doc(doc, cache_hit=True)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/v1/stats", dependencies=[Depends(require_api_key)])
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
