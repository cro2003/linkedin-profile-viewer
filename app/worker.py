"""arq worker: leases an account, fetches, parses, stores.

Retry policy follows the failure taxonomy established during reverse engineering:

- TRANSIENT     backoff and retry, preferring a different account
- SESSION_DEAD  re-mint the cookie jar and retry; never retires the account, because
                a stale jar looks exactly like a ban and usually is not one
- PERMANENT     fail fast, cache the negative result, never retry
"""

import asyncio
import json
import logging

from arq import Retry
from arq.connections import RedisSettings

from app import accounts, logins, metrics, pool, runtime, session, store
from app.config import settings
from app.db import redis
from app.linkedin.client import (
    FetchError,
    PermanentError,
    ProfileClient,
    SessionExpired,
    TransientError,
)
from app.linkedin.parse import ProfileNotInPayload, parse_profile

log = logging.getLogger(__name__)

MAX_TRIES = 4
LEASE_WAIT_SEC = 3
LEASE_ATTEMPTS = 10


def job_id_for(public_id: str) -> str:
    """Stable id so concurrent requests for one profile coalesce onto one job."""
    return f"profile:{public_id}"


async def publish(job_id: str, status: str, **fields) -> None:
    """Progress goes to pubsub for live streaming and to Mongo so a late
    subscriber can replay what it missed."""
    await store.update_job(job_id, status, **fields)
    await redis.publish(f"job:{job_id}", json.dumps({"status": status, **fields}))


async def _lease_with_wait(job_id: str) -> "pool.Account":
    for attempt in range(LEASE_ATTEMPTS):
        account = await pool.lease()
        if account:
            return account
        if attempt == 0:
            await publish(job_id, "waiting_for_account")
        await asyncio.sleep(LEASE_WAIT_SEC)
    raise TransientError("no account available")


async def fetch_profile_job(ctx: dict, public_id: str, force_refresh: bool = False) -> dict:
    # picks up admin config changes without a redeploy
    await runtime.apply_stored()
    job_id = job_id_for(public_id)
    attempt = ctx.get("job_try", 1)

    if not force_refresh:
        cached = await store.get_cached(public_id)
        if cached and cached.get("profile"):
            await publish(job_id, "done", cache_hit=True)
            return {"public_id": public_id, "cache_hit": True}

    account = await _lease_with_wait(job_id)
    await publish(job_id, "fetching", account_id=account.id, attempts=attempt)
    cookies_to_persist = None

    try:
        async with ProfileClient(account) as client:
            try:
                payload = await client.fetch_profile(public_id)
            except SessionExpired as e:
                log.warning("session expired on %s (%s), re-minting", account.id, e)
                await metrics.incr("session_refreshes")
                await publish(job_id, "refreshing_session", account_id=account.id)
                fresh = await session.refresh(account)
                account.cookies = fresh
                async with ProfileClient(account) as retry_client:
                    payload = await retry_client.fetch_profile(public_id)
                    cookies_to_persist = retry_client.cookies
            else:
                cookies_to_persist = client.cookies

        await publish(job_id, "parsing")
        profile, sections, partial = parse_profile(payload, public_id)
        await store.save_profile(profile, payload, sections, partial, account.id)
        await metrics.incr("fetches")
        await publish(job_id, "done", cache_hit=False, account_id=account.id)
        return {"public_id": public_id, "cache_hit": False}

    except (PermanentError, ProfileNotInPayload) as e:
        code = getattr(e, "code", "profile_not_found")
        await metrics.incr("failures")
        await store.save_negative(public_id, code, str(e))
        await publish(job_id, "failed", error={"code": code, "message": str(e)})
        return {"public_id": public_id, "error": code}

    except session.LoginCheckpointRequired as e:
        # not retryable by definition; surface it so an operator can act
        await metrics.incr("checkpoints")
        await metrics.incr("failures")
        await publish(job_id, "failed",
                      error={"code": "account_needs_login", "message": str(e)})
        return {"public_id": public_id, "error": "account_needs_login"}

    except (TransientError, session.SessionRefreshFailed) as e:
        if attempt >= MAX_TRIES:
            await publish(job_id, "failed",
                          error={"code": "upstream_unavailable", "message": str(e)})
            return {"public_id": public_id, "error": "upstream_unavailable"}
        await metrics.incr("retries")
        await publish(job_id, "retrying", error={"message": str(e)}, attempts=attempt)
        raise Retry(defer=min(60, 2 ** attempt * 5))

    except FetchError as e:
        await publish(job_id, "failed", error={"code": "fetch_failed", "message": str(e)})
        return {"public_id": public_id, "error": "fetch_failed"}

    except Exception as e:
        # never leave a job stuck mid-status: an unhandled error still has to be
        # visible to whoever is polling or streaming it
        log.exception("unexpected failure on %s", public_id)
        await publish(job_id, "failed",
                      error={"code": "internal_error", "message": f"{type(e).__name__}: {e}"})
        return {"public_id": public_id, "error": "internal_error"}

    finally:
        await pool.release(account.id, cookies=cookies_to_persist)


async def add_account_job(ctx: dict, login_id: str, account_id: str, email: str,
                          password: str, proxy_url: str | None = None,
                          note: str | None = None) -> dict:
    """Sign in and store the harvested jar. The password is used here and never
    persisted; only cookies are kept."""
    try:
        cookies = await session.login_and_harvest(account_id, email, password,
                                                  proxy_url, login_id)
        await accounts.create(account_id, cookies, proxy_url=proxy_url, email=email,
                              note=note or "added via admin login")
        await pool.set_status(account_id, pool.LIVE)
        await logins.set_status(login_id, logins.DONE, account_id=account_id,
                                cookie_count=len(cookies))
        log.info("account %s added with %d cookies", account_id, len(cookies))
        return {"account_id": account_id, "status": "done"}
    except Exception as e:
        log.error("adding account %s failed: %s", account_id, e)
        await logins.set_status(login_id, logins.FAILED, account_id=account_id,
                                message=str(e)[:300])
        return {"account_id": account_id, "status": "failed"}


async def startup(ctx: dict) -> None:
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await accounts.seed_from_env()
    await accounts.ensure_indexes()
    await store.ensure_indexes()
    await runtime.apply_stored()
    log.info("worker ready with %d usable account(s)", len(await accounts.list_accounts()))


class WorkerSettings:
    functions = [fetch_profile_job, add_account_job]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # one more than our own guard, so the job's terminal state is always published
    # by our code rather than arq abandoning it silently
    max_tries = MAX_TRIES + 1
    job_timeout = 300
    # short: job state lives in Mongo, arq's copy is only needed briefly so that a
    # later request for the same profile can reuse the stable job id
    keep_result = 10
    max_jobs = 4
