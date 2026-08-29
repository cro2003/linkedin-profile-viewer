"""Cookie-jar writes are the dangerous operation here: a partial write destroys a
working session, so the guard is tested directly."""

import pytest

from app import accounts


class FakeCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, query, update, upsert=False):
        self.updates.append((query, update, upsert))
        class Result:
            matched_count = 1
            deleted_count = 1
        return Result()


@pytest.fixture
def fake_collection(monkeypatch):
    fake = FakeCollection()
    monkeypatch.setattr(accounts, "collection", fake)
    return fake


async def test_partial_jar_is_refused(fake_collection):
    assert await accounts.set_cookies("acct1", {"__cf_bm": "abc"}) is False
    assert fake_collection.updates == [], "nothing may be written"


async def test_jar_missing_csrf_is_refused(fake_collection):
    assert await accounts.set_cookies("acct1", {"li_at": "a"}) is False
    assert fake_collection.updates == []


async def test_complete_jar_is_stored(fake_collection):
    assert await accounts.set_cookies(
        "acct1", {"li_at": "a", "JSESSIONID": "ajax:b", "lidc": "c"}) is True
    assert len(fake_collection.updates) == 1


async def test_create_rejects_incomplete_cookies(fake_collection):
    with pytest.raises(ValueError):
        await accounts.create("acct2", {"lidc": "x"})
