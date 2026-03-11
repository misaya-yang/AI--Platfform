from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str


class ReadyResponse(BaseModel):
    status: str
    schema_version: str
    generated_at: str
    modules: dict[str, dict] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    backends: dict[str, str] = Field(default_factory=dict)


class ConfigResponse(BaseModel):
    service_name: str
    schema_version: str
    generated_at: str
    database_schema: str
    default_translation_id: int
    default_recitation_id: int
    sync_all_translations: bool = False
    sync_all_recitations: bool = False
    synced_translation_ids: list[int] = Field(default_factory=list)
    synced_recitation_ids: list[int] = Field(default_factory=list)
    enabled_modules: list[str]
    sync_status: dict[str, str | None]
    cache: dict
    quran_user: dict = Field(default_factory=dict)
    public_endpoints: dict[str, str]
    module_features: dict[str, list[str]]


class ManifestResponse(BaseModel):
    generated_at: str
    schema_version: str
    counts: dict[str, int]
    latest_runs: list[dict]


class CanonicalSummaryResponse(BaseModel):
    database_enabled: bool
    generated_at: str
    counts: dict[str, int]
