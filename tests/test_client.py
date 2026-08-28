"""Client behaviour that must hold without touching the network."""

import httpx
import pytest

from app.config import Account
from app.linkedin.client import (
    PermanentError,
    ProfileClient,
    SessionExpired,
    TransientError,
    classify,
)

ACCOUNT = Account(id="test", cookies={"li_at": "x", "JSESSIONID": "ajax:y"})

PROFILE_PAYLOAD = {
    "included": [{
        "entityUrn": "urn:li:fsd_profile:AAA",
        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
        "publicIdentifier": "someone",
        "firstName": "Some",
        "lastName": "One",
    }]
}


def _transport(profile_status, profile_json=None, probe_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(probe_status, json={})
        return httpx.Response(profile_status, json=profile_json or {})
    return httpx.MockTransport(handler)


def test_status_classification():
    assert classify(200) is None
    assert classify(401) is SessionExpired
    assert classify(999) is SessionExpired
    assert classify(429) is TransientError
    assert classify(503) is TransientError
    assert classify(404) is PermanentError
    # 403 is ambiguous and resolved by a session probe, not by this table
    assert classify(403) is PermanentError


@pytest.mark.asyncio
async def test_success():
    async with ProfileClient(ACCOUNT, transport=_transport(200, PROFILE_PAYLOAD)) as c:
        payload = await c.fetch_profile("someone")
    assert payload["included"][0]["publicIdentifier"] == "someone"


@pytest.mark.asyncio
async def test_403_with_live_session_is_permanent():
    """A missing profile must not be retried, or bad slugs burn every account."""
    async with ProfileClient(ACCOUNT, transport=_transport(403, probe_status=200)) as c:
        with pytest.raises(PermanentError):
            await c.fetch_profile("no-such-person")


@pytest.mark.asyncio
async def test_403_with_dead_session_is_session_expired():
    async with ProfileClient(ACCOUNT, transport=_transport(403, probe_status=403)) as c:
        with pytest.raises(SessionExpired) as e:
            await c.fetch_profile("someone")
    assert e.value.account_dead is True


@pytest.mark.asyncio
async def test_200_without_profile_entity_is_permanent():
    async with ProfileClient(ACCOUNT, transport=_transport(200, {"included": []})) as c:
        with pytest.raises(PermanentError):
            await c.fetch_profile("someone")


@pytest.mark.asyncio
async def test_transient_statuses_are_retryable():
    async with ProfileClient(ACCOUNT, transport=_transport(503)) as c:
        with pytest.raises(TransientError) as e:
            await c.fetch_profile("someone")
    assert e.value.retryable is True
