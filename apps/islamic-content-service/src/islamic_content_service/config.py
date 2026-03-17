from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _fallback(new_key: str, old_key: str, default: str) -> str:
    return os.getenv(new_key, os.getenv(old_key, default))


def _fallback_bool(new_key: str, old_key: str, default: bool) -> bool:
    raw = os.getenv(new_key, os.getenv(old_key))
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _fallback_int(new_key: str, old_key: str, default: int) -> int:
    raw = os.getenv(new_key, os.getenv(old_key))
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _fallback_csv(new_key: str, old_key: str, default: str = "") -> list[str]:
    raw = os.getenv(new_key, os.getenv(old_key, default))
    return [item.strip() for item in raw.split(",") if item.strip()]


def _fallback_int_list(new_key: str, old_key: str) -> list[int]:
    values: list[int] = []
    for item in _fallback_csv(new_key, old_key):
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values


def _quran_api_base_default() -> str:
    explicit = os.getenv("ISLAMIC_CONTENT_QURAN__BASE_URL") or os.getenv(
        "GATEWAY_ISLAMIC_CONTENT__QURAN_BASE_URL"
    )
    if explicit:
        return explicit
    env_name = (
        os.getenv("ISLAMIC_CONTENT_QURAN__ENV")
        or os.getenv("GATEWAY_ISLAMIC_CONTENT__QURAN_ENV")
        or ""
    ).strip().lower()
    auth_url = (
        os.getenv("ISLAMIC_CONTENT_QURAN__AUTH_URL")
        or os.getenv("GATEWAY_ISLAMIC_CONTENT__QURAN_AUTH_URL")
        or ""
    ).strip().lower()
    if env_name == "prelive" or "prelive-oauth2.quran.foundation" in auth_url:
        return "https://apis-prelive.quran.foundation/content/api/v4"
    return "https://apis.quran.foundation/content/api/v4"


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


class AppSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8091
    log_level: str = "INFO"


class DatabaseSettings(BaseModel):
    dsn: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_DATABASE__DSN",
            "GATEWAY_DATABASE__DSN",
            "postgresql://localhost:5432/gateway",
        )
    )
    schema_name: str = Field(default="islamic_content", alias="schema")
    pool_min_size: int = 2
    pool_max_size: int = 10
    auto_migrate: bool = True

    @property
    def schema(self) -> str:
        return self.schema_name


class CacheSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_CACHE__ENABLED",
            "GATEWAY_REDIS__ENABLED",
            True,
        )
    )
    redis_url: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_CACHE__REDIS_URL",
            "GATEWAY_REDIS__URL",
            "redis://localhost:6379/1",
        )
    )
    ttl_seconds: int = 86400
    meta_ttl_seconds: int = 300
    summary_ttl_seconds: int = 60


class QuranSettings(BaseModel):
    base_url: str = ""
    auth_url: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN__AUTH_URL",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_AUTH_URL",
            "https://oauth2.quran.foundation",
        )
    )
    client_id: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN__CLIENT_ID",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_CLIENT_ID",
            "",
        )
    )
    client_secret: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN__CLIENT_SECRET",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_CLIENT_SECRET",
            "",
        )
    )
    access_token: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN__ACCESS_TOKEN",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_ACCESS_TOKEN",
            "",
        )
    )
    scope: str = "content"
    default_translation_id: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__DEFAULT_TRANSLATION_ID",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_DEFAULT_TRANSLATION_ID",
            20,
        )
    )
    default_recitation_id: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__DEFAULT_RECITATION_ID",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_DEFAULT_RECITATION_ID",
            7,
        )
    )
    word_fields: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN__WORD_FIELDS",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_WORD_FIELDS",
            "text_uthmani,text_uthmani_simple,text_imlaei",
        )
    )
    page_size: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__PAGE_SIZE",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_PAGE_SIZE",
            50,
        )
    )
    sync_all_translations: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_QURAN__SYNC_ALL_TRANSLATIONS",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_SYNC_ALL_TRANSLATIONS",
            True,
        )
    )
    sync_all_recitations: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_QURAN__SYNC_ALL_RECITATIONS",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_SYNC_ALL_RECITATIONS",
            True,
        )
    )
    translation_ids: list[int] = Field(
        default_factory=lambda: _fallback_int_list(
            "ISLAMIC_CONTENT_QURAN__TRANSLATION_IDS",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_TRANSLATION_IDS",
        )
    )
    recitation_ids: list[int] = Field(
        default_factory=lambda: _fallback_int_list(
            "ISLAMIC_CONTENT_QURAN__RECITATION_IDS",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_RECITATION_IDS",
        )
    )
    translation_batch_size: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__TRANSLATION_BATCH_SIZE",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_TRANSLATION_BATCH_SIZE",
            10,
        )
    )
    translation_concurrency: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__TRANSLATION_CONCURRENCY",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_TRANSLATION_CONCURRENCY",
            4,
        )
    )
    recitation_concurrency: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_QURAN__RECITATION_CONCURRENCY",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_RECITATION_CONCURRENCY",
            3,
        )
    )

    @model_validator(mode="after")
    def _populate_base_url(self) -> "QuranSettings":
        if self.base_url.strip():
            return self
        if "prelive-oauth2.quran.foundation" in self.auth_url.strip().lower():
            self.base_url = "https://apis-prelive.quran.foundation/content/api/v4"
        else:
            self.base_url = _quran_api_base_default()
        return self


class QuranUserSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_QURAN_USER__ENABLED",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_ENABLED",
            False,
        )
    )
    client_id: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__CLIENT_ID",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_CLIENT_ID",
            "",
        )
    )
    client_secret: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__CLIENT_SECRET",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_CLIENT_SECRET",
            "",
        )
    )
    auth_url: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__AUTH_URL",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_AUTH_URL",
            "https://oauth2.quran.foundation",
        )
    )
    user_api_base_url: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__API_BASE_URL",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_API_BASE_URL",
            "https://api.quran.foundation/api/v1",
        )
    )
    redirect_uri: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__REDIRECT_URI",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_REDIRECT_URI",
            "",
        )
    )
    post_logout_redirect_uri: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_QURAN_USER__POST_LOGOUT_REDIRECT_URI",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_POST_LOGOUT_REDIRECT_URI",
            "",
        )
    )
    scopes: list[str] = Field(
        default_factory=lambda: _fallback_csv(
            "ISLAMIC_CONTENT_QURAN_USER__SCOPES",
            "GATEWAY_ISLAMIC_CONTENT__QURAN_USER_SCOPES",
            "openid,profile,email,offline_access",
        )
    )


class HadithSettings(BaseModel):
    base_url: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_HADITH__BASE_URL",
            "GATEWAY_ISLAMIC_CONTENT__SUNNAH_BASE_URL",
            "https://api.sunnah.com/v1",
        )
    )
    api_key: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_HADITH__API_KEY",
            "GATEWAY_ISLAMIC_CONTENT__SUNNAH_API_KEY",
            "",
        )
    )
    sync_collections: list[str] = Field(
        default_factory=lambda: [
            item.strip()
            for item in _fallback(
                "ISLAMIC_CONTENT_HADITH__SYNC_COLLECTIONS",
                "GATEWAY_ISLAMIC_CONTENT__SUNNAH_SYNC_COLLECTIONS",
                "bukhari,muslim,abudawud",
            ).split(",")
            if item.strip()
        ]
    )
    page_size: int = Field(
        default_factory=lambda: _fallback_int(
            "ISLAMIC_CONTENT_HADITH__PAGE_SIZE",
            "GATEWAY_ISLAMIC_CONTENT__SUNNAH_PAGE_SIZE",
            50,
        )
    )


class DuaSettings(BaseModel):
    data_path: str = Field(
        default_factory=lambda: _fallback(
            "ISLAMIC_CONTENT_DUA__DATA_PATH",
            "GATEWAY_ISLAMIC_CONTENT__DUAS_FILE_PATH",
            "",
        )
    )


class BootstrapSettings(BaseModel):
    on_start: bool = False
    fail_if_empty: bool = False


class ModulesSettings(BaseModel):
    enable_quran: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_MODULES__ENABLE_QURAN",
            "GATEWAY_ISLAMIC_CONTENT__ENABLE_QURAN",
            True,
        )
    )
    enable_hadith: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_MODULES__ENABLE_HADITH",
            "GATEWAY_ISLAMIC_CONTENT__ENABLE_HADITH",
            True,
        )
    )
    enable_dua: bool = Field(
        default_factory=lambda: _fallback_bool(
            "ISLAMIC_CONTENT_MODULES__ENABLE_DUA",
            "GATEWAY_ISLAMIC_CONTENT__ENABLE_DUA",
            True,
        )
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ISLAMIC_CONTENT_",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    quran: QuranSettings = Field(default_factory=QuranSettings)
    quran_user: QuranUserSettings = Field(default_factory=QuranUserSettings)
    hadith: HadithSettings = Field(default_factory=HadithSettings)
    dua: DuaSettings = Field(default_factory=DuaSettings)
    bootstrap: BootstrapSettings = Field(default_factory=BootstrapSettings)
    modules: ModulesSettings = Field(default_factory=ModulesSettings)
