"""Application configuration loaded from environment variables and `.env`."""

from __future__ import annotations

from datetime import timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_DB_PATH = (REPO_ROOT / "data" / "integration.db").as_posix()

_MSK_FALLBACK_TZ = timezone(timedelta(hours=3))


def get_app_timezone(tz_name: str = "Europe/Moscow") -> tzinfo:
    """Return ZoneInfo for the configured timezone with a UTC+3 fallback on minimal OS images."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return _MSK_FALLBACK_TZ


class Settings(BaseSettings):
    """Typed runtime configuration for the integration service."""

    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), str(BACKEND_DIR / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Komtet-RetailCRM Integration"
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"
    http_timeout_seconds: float = 15.0
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ]

    database_url: str = f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"

    # Public service URL / KOMTET webhook callback URL
    public_base_url: str = ""
    komtet_callback_url: str = ""

    # RetailCRM API v5
    retailcrm_url: str = ""
    retailcrm_api_key: SecretStr = SecretStr("")
    retailcrm_default_site: str = ""

    # KOMTET Kassa API
    komtet_base_url: str = "https://kassa.komtet.ru"
    komtet_shop_id: str = ""
    komtet_secret_key: SecretStr = SecretStr("")
    komtet_queue_id: Optional[int] = None
    komtet_api_version: Literal["v1", "v2"] = "v2"

    # Security: shared secrets protecting the admin API and the RetailCRM trigger webhook.
    # In `production` both are mandatory (requests are rejected with 503 when unset).
    admin_api_token: SecretStr = SecretStr("")
    retailcrm_webhook_token: SecretStr = SecretStr("")

    @field_validator(
        "retailcrm_url",
        "komtet_base_url",
        "public_base_url",
        "komtet_callback_url",
        mode="before",
    )
    @classmethod
    def _strip_trailing_slash(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip().rstrip("/")

    @field_validator("komtet_queue_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, value: object) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        return int(str(value).strip())

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> str:
        return str(value or "INFO").strip().upper()

    @property
    def is_production(self) -> bool:
        """Return True when running in the production environment."""
        return self.app_env == "production"

    @property
    def is_retailcrm_configured(self) -> bool:
        """Return True if RetailCRM URL and API key are populated."""
        return bool(self.retailcrm_url and self.retailcrm_api_key.get_secret_value())

    @property
    def resolved_komtet_callback_url(self) -> str:
        """Return explicit KOMTET_CALLBACK_URL or derive it from PUBLIC_BASE_URL."""
        if self.komtet_callback_url:
            return self.komtet_callback_url
        if self.public_base_url:
            return f"{self.public_base_url}/webhooks/komtet"
        return ""

    @property
    def is_komtet_configured(self) -> bool:
        """Return True if KOMTET shop ID and secret key are populated."""
        return bool(
            self.komtet_base_url
            and self.komtet_shop_id
            and self.komtet_secret_key.get_secret_value()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
