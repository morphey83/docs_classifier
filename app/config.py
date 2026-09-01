"""Application settings, loaded from the environment / a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core ---------------------------------------------------------------
    debug: bool = False
    secret_key: str = "dev-only-change-me"
    database_url: str = "postgresql+asyncpg://docs:docs@localhost:5432/docs"
    data_dir: Path = Path("./data")

    # --- auth --------------------------------------------------------------
    session_ttl_hours: int = 24 * 14
    session_cookie_name: str = "dcsid"
    cookie_secure: bool = True  # set False for plain-HTTP local dev

    # --- quota defaults (global hard caps; see docs/architecture.md §10) --
    max_upload_mb: int = 200
    max_archive_entries: int = 2000
    max_archive_unpacked_mb: int = 2000
    max_archive_depth: int = 2
    default_domain_quota_mb: int = 5000
    default_trash_retention_days: int = 30
    export_ttl_hours: int = 48

    # --- worker ----------------------------------------------------------
    redis_url: str | None = None  # unused; SAQ runs on Postgres

    @property
    def sync_database_url(self) -> str:
        """psycopg2/psycopg URL for Alembic and the SAQ Postgres queue."""
        return self.database_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
