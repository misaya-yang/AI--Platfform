"""Request-scoped model registry backed by the tenant provider control plane."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ai_gateway_core.enums import ModelProvider
from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import decrypt_value, is_encrypted

from .model_registry import ModelRegistry

logger = get_logger(__name__)


def _runtime_provider(row: Mapping[str, Any]) -> ModelProvider | None:
    api_type = str(row.get("api_type") or "").strip().lower()
    provider_id = str(row.get("provider_id") or "").strip().lower()
    base_url = str(row.get("base_url") or "").strip().lower()

    if api_type in {"google-vertex", "vertex"} or provider_id.startswith("google-vertex"):
        return ModelProvider.GOOGLE_VERTEX
    if api_type in {"google", "google-ai-studio"}:
        return ModelProvider.GOOGLE
    if (
        api_type in {"dashscope", "aliyun"}
        or provider_id.startswith("dashscope")
        or "dashscope.aliyuncs.com" in base_url
    ):
        return ModelProvider.DASHSCOPE
    if api_type == "anthropic" or provider_id.startswith("anthropic"):
        return ModelProvider.ANTHROPIC
    if provider_id.startswith("deepseek") or "api.deepseek.com" in base_url:
        return ModelProvider.DEEPSEEK
    if api_type in {"openai", "openai-compatible"}:
        return ModelProvider.OPENAI
    return None


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class TenantModelRegistryResolver:
    """Build an isolated registry when a tenant saved provider credentials in the UI.

    The process-wide environment registry remains the compatibility fallback.
    Tenant secrets are never installed into that shared registry, preventing one
    tenant's provider configuration from racing with another request.
    """

    def __init__(self, database: Any, *, encryption_key: str = "") -> None:
        self.database = database
        self.encryption_key = encryption_key

    async def resolve(self, tenant_id: str, model_id: str) -> ModelRegistry | None:
        if self.database is None:
            return None

        try:
            row = await self.database.fetchrow(
                """
                SELECT m.model_id, m.display_name, m.context_window,
                       m.max_output_tokens, m.supports_vision, m.supports_tools,
                       m.input_price_per_1k, m.output_price_per_1k, m.access_level,
                       p.provider_id, p.api_type, p.base_url,
                       p.api_key_encrypted, p.metadata
                FROM llm_models AS m
                JOIN llm_providers AS p
                  ON p.tenant_id = m.tenant_id
                 AND p.provider_id = m.provider_id
                WHERE m.tenant_id = $1
                  AND m.model_id = $2
                  AND m.is_enabled = true
                  AND p.is_enabled = true
                ORDER BY m.sort_order ASC, p.provider_id ASC
                LIMIT 1
                """,
                tenant_id or "default",
                model_id,
            )
        except Exception as exc:
            logger.warning(
                "Tenant model registry lookup failed (exception_type=%s)",
                type(exc).__name__,
            )
            return None

        if not row:
            return None
        data = dict(row)
        stored_secret = str(data.get("api_key_encrypted") or "")
        if not stored_secret:
            # This is the normal env/CLI configuration path.
            return None

        registry = ModelRegistry(use_default_models=False)
        provider = _runtime_provider(data)
        if provider is None:
            logger.warning("Tenant provider type is unsupported by Assistant runtime")
            return registry

        loaded = registry.replace_models_from_database_rows(
            [{**data, "provider_id": provider.value}],
            default_context_window=128000,
            default_max_output_tokens=4096,
        )
        if loaded != 1:
            logger.warning("Tenant model metadata is invalid")
            return registry

        api_key = decrypt_value(stored_secret, self.encryption_key)
        if is_encrypted(api_key):
            logger.error("Tenant provider credential could not be decrypted")
            return registry

        metadata = _metadata(data.get("metadata"))
        wire_protocol = str(metadata.get("wire_protocol") or "chat_completions")
        try:
            registry.configure_provider(
                provider,
                api_key=api_key,
                base_url=str(data.get("base_url") or "").strip() or None,
                backend="vertex" if provider is ModelProvider.GOOGLE_VERTEX else "ai_studio",
                wire_protocol=wire_protocol,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Tenant provider configuration is invalid (exception_type=%s)",
                type(exc).__name__,
            )
        return registry


__all__ = ["TenantModelRegistryResolver"]
