"""Application settings sourced from environment variables and an optional .env file."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load application settings without opening a database connection."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DreamPlanner API"
    database_url: str | None = None
    migration_database_url: str | None = None
    environment: Literal["local", "production"] = "local"
    jwt_signing_key_base64: SecretStr | None = None
    jwt_previous_signing_key_base64: SecretStr | None = None
    jwt_current_kid: str | None = None
    jwt_previous_kid: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    refresh_cookie_name: str = "dreamplanner_refresh"
    refresh_cookie_path: str = "/api/v1/auth/sessions/current"
    refresh_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    refresh_cookie_secure: bool = False
    rate_limit_storage_uri: str = "memory://"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )

    @field_validator("cors_origins")
    @classmethod
    def reject_wildcard_origins(cls, origins: list[str]) -> list[str]:
        """Require explicit origins when browser credentials are enabled.

        Raises:
            ValueError: If the list contains a wildcard origin.
        """
        if "*" in origins:
            raise ValueError("CORS origins must be explicit")
        return origins

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        """Reject unsafe cookie or rate-limit settings before serving requests."""
        if self.rate_limit_storage_uri != "memory://":
            raise ValueError("RATE_LIMIT_STORAGE_URI must be memory:// for this MVP")
        if self.environment == "production" and not self.refresh_cookie_secure:
            raise ValueError("Production refresh cookies must be Secure")
        if self.refresh_cookie_samesite == "none" and not self.refresh_cookie_secure:
            raise ValueError("SameSite=None requires Secure refresh cookies")
        if not self.refresh_cookie_path.startswith("/api/v1/auth/"):
            raise ValueError("Refresh cookie Path must be under /api/v1/auth/")
        if not self.refresh_cookie_name.isidentifier():
            raise ValueError("Refresh cookie name must be an identifier")
        if self.environment == "production" and any(
            origin.startswith("http://") for origin in self.cors_origins
        ):
            raise ValueError("Production CORS origins must use HTTPS")
        return self
