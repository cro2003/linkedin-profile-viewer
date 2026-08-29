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


def payload_for(public_id, urn="urn:li:fsd_profile:AAA", extra_profiles=()):
    """A response shaped like the real thing: `data.*elements` names the queried
    profile, while `included` may also hold unrelated people."""
    profiles = [
        {
            "entityUrn": other_urn,
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "publicIdentifier": other_id,
            "firstName": "Other",
            "lastName": "Person",
        }
        for other_id, other_urn in extra_profiles
    ]
    profiles.append(
        {
            "entityUrn": urn,
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "publicIdentifier": public_id,
            "firstName": "Some",
            "lastName": "One",
        }
    )
    return {
        "data": {
            "$type": "com.linkedin.restli.common.CollectionResponse",
            "*elements": [urn],
        },
        "included": profiles,
    }


PROFILE_PAYLOAD = payload_for("someone")


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


def _identity_transport(returned_id):
    payload = payload_for(returned_id, urn="urn:li:fsd_profile:BBB")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_mismatched_identity_is_refused():
    """Upstream answers an unresolvable id with a different person; never serve it."""
    from app.linkedin.client import IdentityMismatch

    async with ProfileClient(ACCOUNT, transport=_identity_transport("someone-else-9f8e7d")) as c:
        with pytest.raises(IdentityMismatch):
            await c.fetch_profile("reidhoffman")


async def test_identity_comparison_ignores_case():
    async with ProfileClient(ACCOUNT, transport=_identity_transport("someone")) as c:
        payload = await c.fetch_profile("SomeOne")
    assert payload["included"][0]["publicIdentifier"] == "someone"


async def test_persisted_jar_keeps_seeded_cookies():
    """Regression: a domain filter dropped seeded cookies and wiped the session."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=PROFILE_PAYLOAD,
            headers={"set-cookie": "__cf_bm=abc; Domain=.linkedin.com; Path=/"},
        )

    async with ProfileClient(ACCOUNT, transport=httpx.MockTransport(handler)) as c:
        await c.fetch_profile("someone")
        jar = c.cookies

    assert jar["li_at"] == "x", "seeded session cookie must survive"
    assert jar["JSESSIONID"] == "ajax:y"
    assert jar["__cf_bm"] == "abc", "server-set cookie must be captured"


async def test_picks_the_queried_profile_not_a_bystander():
    """Live bug: the decoration also returns "people also viewed", and taking the
    first Profile entity in `included` handed back a stranger."""
    payload = payload_for(
        "queried-user-1a2b3c",
        urn="urn:li:fsd_profile:WANTED",
        extra_profiles=[("bystander-user-4d5e6f", "urn:li:fsd_profile:BYSTANDER")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    from app.linkedin.parse import primary_profile

    async with ProfileClient(ACCOUNT, transport=httpx.MockTransport(handler)) as c:
        result = await c.fetch_profile("queried-user-1a2b3c")

    chosen = primary_profile(result)
    assert chosen["publicIdentifier"] == "queried-user-1a2b3c"
    assert chosen["entityUrn"] == "urn:li:fsd_profile:WANTED"


async def test_empty_elements_is_not_found():
    payload = {"data": {"*elements": []}, "included": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with ProfileClient(ACCOUNT, transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(PermanentError):
            await c.fetch_profile("nobody")
