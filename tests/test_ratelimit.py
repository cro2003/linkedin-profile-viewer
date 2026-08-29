"""Client identity is the security-sensitive half of rate limiting: get it wrong
and any caller can dodge the limit by sending a header."""

from dataclasses import dataclass

from app.config import settings
from app.ratelimit import client_identity


@dataclass
class FakeClient:
    host: str


class FakeRequest:
    def __init__(self, host="10.0.0.1", headers=None):
        self.client = FakeClient(host)
        self.headers = headers or {}


def test_api_key_identifies_the_caller():
    identity = client_identity(FakeRequest(), "supersecretkey12345")
    assert identity.startswith("key:")
    assert "supersecretkey12345" not in identity, "full key must not become a redis key"


def test_falls_back_to_peer_ip():
    assert client_identity(FakeRequest(host="203.0.113.9"), None) == "ip:203.0.113.9"


def test_forwarded_header_ignored_when_not_behind_proxy(monkeypatch):
    """Otherwise a caller sets X-Forwarded-For per request and is never limited."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    request = FakeRequest(host="203.0.113.9", headers={"x-forwarded-for": "1.2.3.4"})
    assert client_identity(request, None) == "ip:203.0.113.9"


def test_forwarded_header_used_when_behind_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    request = FakeRequest(host="10.0.0.1",
                          headers={"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
    assert client_identity(request, None) == "ip:1.2.3.4"


def test_missing_client_does_not_crash(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    request = FakeRequest()
    request.client = None
    assert client_identity(request, None) == "ip:unknown"
