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
    set_archive_ttl_days: int = 7  # set-archive cache file lifetime
    public_download_rate_per_min: int = 60  # per-IP cap on GET /d/{token}
    default_allowed_types: str | None = None  # comma-separated extensions; unset = unrestricted
    tg_link_ttl_minutes: int = 15

    # --- absolute links & the bot ------------------------------------------
    # Scheme+host used to build every absolute link the app hands out (share
    # links, the bot's deep-links, the account-linking page). Unset -> links
    # stay relative. The one setting that changes when a real (sub)domain
    # replaces the bare VDS IP.
    public_base_url: str | None = None
    telegram_bot_username: str | None = None  # without '@', for t.me/<name> deep-links
    bot_token: str | None = None  # from @BotFather — the `bot` service (§8)
    bot_search_page_size: int = 5

    @property
    def default_allowed_types_set(self) -> set[str] | None:
        if not self.default_allowed_types:
            return None
        parts = self.default_allowed_types.split(",")
        return {p.strip().lower().lstrip(".") for p in parts if p.strip()}

    # --- jobs / worker --------------------------------------------------
    # "queue": push to the SAQ (Postgres) queue for a worker to run.
    # "inline": run in a FastAPI BackgroundTask (dev without a worker; tests).
    job_mode: str = "queue"
    ocr_default_lang: str = "rus+eng"
    ocr_image_max_px: int = 2500

    @property
    def sync_database_url(self) -> str:
        """psycopg2/psycopg URL for Alembic and the SAQ Postgres queue."""
        return self.database_url.replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
