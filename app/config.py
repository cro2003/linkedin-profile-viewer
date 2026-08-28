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
    # only used to re-login when the persisted browser profile cannot self-heal;
    # the usual refresh path needs no credentials at all
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
    browser_profile_dir: str = "/data/browser"
    browser_headless: bool = True
    cookie_refresh_timeout_sec: float = 90
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
