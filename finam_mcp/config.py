from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets are never included in repr output."""

    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    collector_base_url: AnyHttpUrl = Field(default=AnyHttpUrl("http://finam-collector:8080"))
    research_api_token: SecretStr = Field(default=SecretStr(""))

    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    mcp_resource_url: AnyHttpUrl
    oauth_issuer_url: AnyHttpUrl
    oauth_jwks_url: AnyHttpUrl | None = None
    oauth_audience: str | None = None
    mcp_required_scope: str = "finam:read"

    http_timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)
    freshness_seconds: int = Field(default=180, ge=30, le=3600)
    rate_limit_requests: int = Field(default=60, ge=1, le=1000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("mcp_resource_url", "oauth_issuer_url")
    @classmethod
    def require_https_for_public_urls(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        host = value.host or ""
        if value.scheme != "https" and host not in {"localhost", "127.0.0.1", "testserver"}:
            raise ValueError("public MCP and OAuth URLs must use HTTPS")
        return value

    @model_validator(mode="after")
    def fill_derived_values(self) -> Settings:
        issuer = str(self.oauth_issuer_url).rstrip("/")
        if self.oauth_jwks_url is None:
            self.oauth_jwks_url = AnyHttpUrl(f"{issuer}/protocol/openid-connect/certs")
        if not self.oauth_audience:
            self.oauth_audience = str(self.mcp_resource_url).rstrip("/")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
