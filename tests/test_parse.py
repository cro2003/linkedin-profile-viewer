"""Parser tests run against captured payloads in fixtures/raw/.

Those fixtures hold real profile data so they are gitignored; the tests skip
rather than fail on a fresh clone. Capture one with research/probe.py.
"""

import json
from pathlib import Path

import pytest

from app.linkedin.parse import ProfileNotInPayload, parse_profile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "raw"


def _payloads():
    if not FIXTURES.exists():
        return []
    out = []
    for f in sorted(FIXTURES.glob("*.json")):
        try:
            payload = json.loads(f.read_text())
        except ValueError:
            continue
        # only full-profile payloads: the vanity->urn resolver returns a bare urn
        if any(
            e.get("$type", "").endswith("identity.profile.Profile") and e.get("publicIdentifier")
            for e in payload.get("included", [])
        ):
            out.append(pytest.param(payload, id=f.stem[:40]))
    return out


PAYLOADS = _payloads()
needs_fixtures = pytest.mark.skipif(not PAYLOADS, reason="no captured fixtures available")


@needs_fixtures
@pytest.mark.parametrize("payload", PAYLOADS)
def test_parses_core_fields(payload):
    profile, _sections, partial = parse_profile(payload)
    assert partial == [], f"sections failed to parse: {partial}"
    assert profile.public_id
    assert profile.url.endswith(profile.public_id)
    assert profile.full_name


@needs_fixtures
@pytest.mark.parametrize("payload", PAYLOADS)
def test_section_metadata_reports_truncation(payload):
    """A collection returns only its first page, so `complete` must reflect that
    rather than silently handing back a partial list."""
    _, sections, _ = parse_profile(payload)
    for name in ("skills", "certifications", "languages"):
        info = sections[name]
        assert info.returned >= 0
        if info.total is not None:
            assert info.complete == (info.returned >= info.total)


@needs_fixtures
@pytest.mark.parametrize("payload", PAYLOADS)
def test_sections_are_well_formed(payload):
    profile, _sections, _ = parse_profile(payload)
    for job in profile.experience:
        assert job.title or job.company
        if job.is_current:
            assert job.date_range and not job.date_range.end
    for school in profile.education:
        assert school.school
    flags = [j.is_current for j in profile.experience]
    assert flags == sorted(flags, reverse=True)
    for current in (True, False):
        years = [
            j.date_range.start.year
            for j in profile.experience
            if j.is_current is current
            and j.date_range
            and j.date_range.start
            and j.date_range.start.year
        ]
        assert years == sorted(years, reverse=True)


def test_empty_payload_is_rejected():
    with pytest.raises(ProfileNotInPayload):
        parse_profile({"included": []})
