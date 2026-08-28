"""Pool guards that must hold without a live Redis."""

import pytest

from app import pool


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(pool, "redis", fake)
    return fake


async def test_partial_jar_is_not_persisted(fake_redis):
    """Storing a jar without the session cookies would break every later lease."""
    await pool.set_cookies("acct1", {"__cf_bm": "abc"})
    assert fake_redis.store == {}


async def test_complete_jar_is_persisted(fake_redis):
    await pool.set_cookies("acct1", {"li_at": "a", "JSESSIONID": "ajax:b", "lidc": "c"})
    assert "acct:acct1:cookies" in fake_redis.store
