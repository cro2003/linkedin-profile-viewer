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

    async def exists(self, key):
        return key in self.store

    async def delete(self, key):
        self.store.pop(key, None)


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


async def test_lease_preserves_all_account_fields(fake_redis, monkeypatch):
    """Regression: rebuilding the account by hand dropped the login credentials."""
    from app.config import Account, settings

    configured = Account(id="acct1", cookies={"li_at": "seed", "JSESSIONID": "ajax:s"},
                         proxy_url="http://proxy:8080", email="a@b.c", password="pw")
    monkeypatch.setattr(settings, "linkedin_accounts", [configured])
    await pool.set_cookies("acct1", {"li_at": "fresh", "JSESSIONID": "ajax:f"})

    leased = await pool.lease()
    assert leased is not None
    assert leased.cookies["li_at"] == "fresh", "live jar must win over the seed"
    assert leased.email == "a@b.c"
    assert leased.password == "pw"
    assert leased.proxy_url == "http://proxy:8080"


async def test_lease_skips_locked_account(fake_redis, monkeypatch):
    from app.config import Account, settings

    monkeypatch.setattr(settings, "linkedin_accounts",
                        [Account(id="acct1", cookies={"li_at": "a", "JSESSIONID": "ajax:b"})])
    first = await pool.lease()
    assert first is not None
    assert await pool.lease() is None, "one in-flight request per account"
