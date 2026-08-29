"""Job document lifecycle.

A refresh reuses the same job id, so a completed run leaves a terminal document
behind. If that is not reset, anyone polling or streaming the new run is handed the
previous run's "done" straight away.
"""

import pytest

from app import store


class FakeJobs:
    def __init__(self):
        self.doc = None
        self.replaced = 0

    async def replace_one(self, query, doc, upsert=False):
        self.doc = doc
        self.replaced += 1

    async def find_one(self, query):
        return self.doc


@pytest.fixture
def fake_jobs(monkeypatch):
    fake = FakeJobs()
    monkeypatch.setattr(store, "jobs", fake)
    return fake


async def test_start_job_resets_a_completed_run(fake_jobs):
    fake_jobs.doc = {
        "_id": "profile:someone",
        "public_id": "someone",
        "status": "done",
        "events": [{"status": "queued"}, {"status": "done"}],
        "error": {"code": "stale"},
        "attempts": 3,
    }
    doc = await store.start_job("profile:someone", "someone")

    assert doc["status"] == "queued", "a new run must not inherit the old terminal state"
    assert [e["status"] for e in doc["events"]] == ["queued"]
    assert doc["error"] is None
    assert doc["attempts"] == 0


async def test_start_job_creates_when_absent(fake_jobs):
    doc = await store.start_job("profile:nobody", "nobody")
    assert doc["status"] == "queued"
    assert fake_jobs.replaced == 1
