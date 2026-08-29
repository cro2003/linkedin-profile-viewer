"""Image proxy host allowlist.

This endpoint fetches a URL supplied by the caller, so the allowlist is the only
thing standing between it and a server-side request forgery. Every rejection below
happens before any network call is made.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.media import router

app = FastAPI()
app.include_router(router)
client = TestClient(app)

REJECTED = [
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://127.0.0.1:8000/v1/stats",  # loopback
    "http://mongo:27017/",  # internal service name
    "https://evil.example.com/x.png",  # arbitrary host
    "http://media.licdn.com/x.png",  # right host, plain http
    "https://media.licdn.com.evil.com/x.png",  # suffix-lookalike host
    "https://notmedia.licdn.com/x.png",  # subdomain not on the list
]


@pytest.mark.parametrize("url", REJECTED)
def test_rejects_disallowed_urls(url):
    response = client.get("/v1/media", params={"u": url})
    assert response.status_code == 400
    # this bare app has no error handler registered, so the payload arrives under
    # `detail`; the running app rewrites it to `error`
    body = response.json()
    assert (body.get("error") or body.get("detail"))["code"] == "host_not_allowed"


def test_requires_the_url_parameter():
    assert client.get("/v1/media").status_code == 422
