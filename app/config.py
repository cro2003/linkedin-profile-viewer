"""Settings, loaded from the environment.

Values here are defaults and first-boot seeds. LinkedIn accounts live in Mongo
(app/accounts.py) and rate limits can be overridden at runtime (app/runtime.py),
so the environment is not the last word on either.
"""

import json
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Routing cookies are not optional: without them LinkedIn's edge answers every
# request with a 302 to the identical URL, forever.
REQUIRED_COOKIES = ("li_at", "JSESSIONID")


class Account(BaseModel):
    id: str
    cookies: dict[str, str]
    proxy_url: str | None = None
    # only used to re-login when the browser profile cannot self-heal
    email: str | None = None
    password: str | None = None

    @field_validator("cookies")
    @classmethod
    def _require_core(cls, v):
        missing = [c for c in REQUIRED_COOKIES if not v.get(c)]
        if missing:
            raise ValueError(f"account cookies missing {missing}")
        return v

    @property
    def csrf_token(self) -> str:
        return self.cookies["JSESSIONID"].strip('"')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "INFO"

    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "linkedin"
    redis_url: str = "redis://localhost:6379"

    linkedin_accounts: list[Account] = Field(default_factory=list)

    account_min_delay_sec: float = 8
    account_max_delay_sec: float = 20
    proxy_required: bool = False
    rate_limit_write_per_min: int = 10
    rate_limit_read_per_min: int = 60
    trust_proxy_headers: bool = False

    superadmin_email: str | None = None
    superadmin_password: str | None = None
    session_ttl_days: int = 7
    session_cookie_name: str = "sourcely_session"
    anon_cookie_name: str = "sourcely_anon"
    # per-browser free quota, plus a looser per-IP cap so clearing cookies
    # does not reset it while shared NAT is not punished
    anon_free_lookups: int = 5
    anon_ip_lookups: int = 15
    # set true once served over HTTPS so cookies are not sent in the clear
    cookie_secure: bool = False
    sse_max_duration_sec: int = 300
    browser_profile_dir: str = "/data/browser"
    browser_headless: bool = True
    cookie_refresh_timeout_sec: float = 90
    otp_wait_sec: int = 600
    cache_ttl_hours: int = 24
    negative_cache_ttl_hours: int = 1
    request_timeout_sec: float = 30

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, v):
        return [k.strip() for k in v.split(",") if k.strip()] if isinstance(v, str) else v

    @field_validator("linkedin_accounts", mode="before")
    @classmethod
    def _parse_accounts(cls, v):
        return json.loads(v) if isinstance(v, str) else v


settings = Settings()
