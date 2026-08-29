"""Password, API key and quota logic. No database needed."""

import pytest
from fastapi import HTTPException

from app import auth
from app.config import settings


def test_password_round_trip():
    digest, salt = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", digest, salt)
    assert not auth.verify_password("wrong password", digest, salt)


def test_same_password_gets_different_salts():
    """Otherwise identical passwords share a hash and leak that fact."""
    first, salt_a = auth.hash_password("same-password")
    second, salt_b = auth.hash_password("same-password")
    assert salt_a != salt_b
    assert first != second


def test_password_is_not_recoverable_from_hash():
    digest, salt = auth.hash_password("secret-value")
    assert "secret-value" not in digest
    assert "secret-value" not in salt


def test_api_key_hashing():
    key, key_hash, prefix = auth.new_api_key()
    assert key.startswith("lpg_")
    assert prefix == key[:12]
    assert auth.hash_api_key(key) == key_hash
    assert key not in key_hash, "stored hash must not contain the key"
    assert auth.hash_api_key("lpg_other") != key_hash


@pytest.mark.parametrize("email", ["nope", "no@domain", "@example.com", "", "a b@c.com"])
def test_rejects_bad_emails(email):
    with pytest.raises(HTTPException) as e:
        auth.validate_credentials(email, "longenoughpassword")
    assert e.value.detail["code"] == "invalid_email"


def test_rejects_short_password():
    with pytest.raises(HTTPException) as e:
        auth.validate_credentials("user@example.com", "short")
    assert e.value.detail["code"] == "weak_password"


def test_email_is_normalised():
    assert (
        auth.validate_credentials("  User@Example.COM ", "longenoughpassword") == "user@example.com"
    )


class FakeRedis:
    def __init__(self, values=None):
        self.values = values or {}

    async def mget(self, keys):
        return [self.values.get(k) for k in keys]


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(auth, "redis", fake)
    return fake


async def test_quota_allows_until_browser_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "anon_free_lookups", 5)
    fake_redis.values = {"anon:abc": "4", "anonip:1.2.3.4": "4"}
    assert await auth.check_anon_quota("abc", "1.2.3.4") == 1


async def test_quota_blocks_at_browser_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "anon_free_lookups", 5)
    fake_redis.values = {"anon:abc": "5", "anonip:1.2.3.4": "0"}
    with pytest.raises(HTTPException) as e:
        await auth.check_anon_quota("abc", "1.2.3.4")
    assert e.value.detail["code"] == "signup_required"


async def test_quota_blocks_on_ip_even_with_fresh_cookie(fake_redis, monkeypatch):
    """Clearing cookies must not reset the free quota."""
    monkeypatch.setattr(settings, "anon_free_lookups", 5)
    monkeypatch.setattr(settings, "anon_ip_lookups", 15)
    fake_redis.values = {"anon:brand-new": None, "anonip:1.2.3.4": "15"}
    with pytest.raises(HTTPException):
        await auth.check_anon_quota("brand-new", "1.2.3.4")


def test_caller_roles():
    admin = auth.Caller("user", {"role": "superadmin", "_id": "x"}, "user:x")
    plain = auth.Caller("user", {"role": "user", "_id": "y"}, "user:y")
    anon = auth.Caller("anon")
    assert admin.is_superadmin and admin.is_authenticated
    assert plain.is_authenticated and not plain.is_superadmin
    assert not anon.is_authenticated and not anon.is_superadmin
