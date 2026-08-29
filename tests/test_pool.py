"""Scheduling guards that must hold without a live Redis or Mongo."""

import pytest

from app import accounts, pool
from app.config import Account


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

    async def noop(*args, **kwargs):
        return None

    # release() records last-used in Mongo; these tests must not touch a database
    monkeypatch.setattr(accounts, "touch", noop)
    monkeypatch.setattr(accounts, "set_cookies", noop)
    return fake


def _use_accounts(monkeypatch, *account_list):
    async def list_accounts(include_disabled=False):
        return list(account_list)

    monkeypatch.setattr(accounts, "list_accounts", list_accounts)


async def test_lease_returns_account_with_its_jar(fake_redis, monkeypatch):
    account = Account(
        id="acct1",
        cookies={"li_at": "fresh", "JSESSIONID": "ajax:f"},
        proxy_url="http://proxy:8080",
        email="a@b.c",
    )
    _use_accounts(monkeypatch, account)

    leased = await pool.lease()
    assert leased is not None
    assert leased.cookies["li_at"] == "fresh"
    assert leased.proxy_url == "http://proxy:8080"
    assert leased.email == "a@b.c"


async def test_lease_serialises_one_request_per_account(fake_redis, monkeypatch):
    _use_accounts(monkeypatch, Account(id="acct1", cookies={"li_at": "a", "JSESSIONID": "ajax:b"}))
    assert await pool.lease() is not None
    assert await pool.lease() is None, "one in-flight request per account"


async def test_lease_skips_unusable_accounts(fake_redis, monkeypatch):
    _use_accounts(monkeypatch, Account(id="acct1", cookies={"li_at": "a", "JSESSIONID": "ajax:b"}))
    await pool.set_status("acct1", pool.NEEDS_LOGIN)
    assert await pool.lease() is None, "an account awaiting human login must be skipped"


async def test_release_starts_a_cooldown(fake_redis, monkeypatch):
    _use_accounts(monkeypatch, Account(id="acct1", cookies={"li_at": "a", "JSESSIONID": "ajax:b"}))
    await pool.lease()
    await pool.release("acct1")
    assert await pool.lease() is None, "account must cool down before reuse"
