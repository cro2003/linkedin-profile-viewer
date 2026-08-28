import json
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Account(BaseModel):
    id: str
    li_at: str
    jsessionid: str
    proxy_url: str | None = None

    @property
    def csrf_token(self) -> str:
        return self.jsessionid.strip('"')

    @property
    def cookie_header(self) -> str:
        return f'li_at={self.li_at}; JSESSIONID="{self.csrf_token}"'


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
    cache_ttl_hours: int = 24
    negative_cache_ttl_hours: int = 1

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_keys(cls, v):
        return [k.strip() for k in v.split(",") if k.strip()] if isinstance(v, str) else v

    @field_validator("linkedin_accounts", mode="before")
    @classmethod
    def _parse_accounts(cls, v):
        return json.loads(v) if isinstance(v, str) else v


settings = Settings()
