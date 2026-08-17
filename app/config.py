"""
config.py — Application configuration.

Reads environment variables (or a .env file) using Pydantic BaseSettings.
All settings are validated at startup, so missing required vars raise a
clear error before the app accepts any traffic.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    # Full async connection string for SQLAlchemy + asyncpg.
    # Example: postgresql+asyncpg://postgres:password@localhost:5432/linkplease
    DATABASE_URL: str

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_database_url_scheme(cls, v: str) -> str:
        if not v:
            return v
        
        # Strip whitespace and potential accidental quotes added in dashboards
        v = v.strip().strip("'").strip('"')
        
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # ------------------------------------------------------------------ #
    # PseudoGram
    # ------------------------------------------------------------------ #
    # Your API key for PseudoGram.
    # This key is used for TWO things:
    #   1. The X-Api-Key header on every outgoing HTTP request to PseudoGram.
    #   2. The HMAC-SHA256 secret when verifying incoming webhook signatures.
    PSEUDOGRAM_API_KEY: str

    # Base URL for PseudoGram API.  Can be overridden in .env if the URL
    # ever changes or you want to point at a local mock during development.
    PSEUDOGRAM_BASE_URL: str = "https://pseudogram-api.onrender.com"

    # ------------------------------------------------------------------ #
    # Worker tuning (sensible defaults — no need to set these in .env)
    # ------------------------------------------------------------------ #
    # How many seconds the worker sleeps between DB poll iterations.
    WORKER_POLL_INTERVAL: float = 1.0

    # How many seconds between each reconciliation poll of GET /v1/dm/{dm_id}.
    DM_RECONCILE_INTERVAL: float = 2.0

    # Maximum send attempts before permanently marking a delivery as failed.
    MAX_RETRIES: int = 5

    # ------------------------------------------------------------------ #
    # Pydantic settings configuration
    # ------------------------------------------------------------------ #
    model_config = SettingsConfigDict(
        # Load a .env file if it exists.  Safe to skip if env vars are set
        # another way (Docker, CI, etc.).
        env_file=".env",
        env_file_encoding="utf-8",
    )


# Single global instance — import this everywhere instead of re-reading env.
settings = Settings()
