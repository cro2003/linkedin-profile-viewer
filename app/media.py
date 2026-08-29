"""Image proxy.

Profile photos and company logos live on LinkedIn's CDN, which sits on common
tracker blocklists — so a visitor running an ad blocker sees empty placeholders even
though the URLs are perfectly valid. Serving them through our own host makes them
first-party requests, which host-based blocking does not touch.

The API still returns the original CDN URLs; only the web UI routes through here.
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["media"])

# an allowlist, not a pattern: this endpoint takes a URL from the caller, so
# anything looser is a server-side request forgery hole
ALLOWED_HOSTS = {"media.licdn.com", "static.licdn.com"}
ALLOWED_TYPES = ("image/",)
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SEC = 15
CACHE_SECONDS = 86400


@router.get("/media")
async def proxy_media(u: str = Query(..., description="CDN image URL to fetch")):
    parsed = httpx.URL(u)
    if parsed.scheme != "https" or parsed.host not in ALLOWED_HOSTS:
        raise HTTPException(
            400,
            {
                "code": "host_not_allowed",
                "message": f"only https images from {', '.join(sorted(ALLOWED_HOSTS))}",
            },
        )

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC, follow_redirects=True) as client:
            upstream = await client.get(str(parsed), headers={"accept": "image/*"})
    except httpx.HTTPError as e:
        raise HTTPException(
            502, {"code": "media_unavailable", "message": type(e).__name__, "retryable": True}
        ) from e

    content_type = upstream.headers.get("content-type", "")
    if upstream.status_code != 200 or not content_type.startswith(ALLOWED_TYPES):
        raise HTTPException(
            404,
            {
                "code": "media_not_found",
                "message": f"upstream returned {upstream.status_code} {content_type}",
            },
        )
    if len(upstream.content) > MAX_BYTES:
        raise HTTPException(413, {"code": "media_too_large", "message": "image exceeds 8MB"})

    return Response(
        content=upstream.content,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"},
    )
