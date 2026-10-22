"""Application settings sourced from environment variables and an optional .env file."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load application settings without opening a database connection."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DreamPlanner API"
    database_url: str | None = None
    migration_database_url: str | None = None
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
