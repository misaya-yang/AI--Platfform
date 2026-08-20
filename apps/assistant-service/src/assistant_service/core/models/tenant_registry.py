"""Request-scoped model registry backed by the tenant provider control plane."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from ai_gateway_core.enums import ModelProvider
from ai_gateway_core.logging import get_logger, record_internal_exception
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

    def __init__(
        self,
        database: Any,
        *,
        encryption_key: str = "",
        cache_ttl_seconds: float = 30.0,
        cache_max_entries: int = 128,
    ) -> None:
        self.database = database
        self.encryption_key = encryption_key
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self.cache_max_entries = max(1, int(cache_max_entries))
        self._cache: OrderedDict[tuple[str, str, str | None], tuple[float, ModelRegistry]] = (
            OrderedDict()
        )
        self._locks: dict[tuple[str, str, str | None], asyncio.Lock] = {}
        self._active_leases: dict[int, int] = {}
        self._retired_registries: dict[int, ModelRegistry] = {}

    def retain(self, registry: ModelRegistry) -> None:
        """Keep a resolved snapshot alive for the duration of one run."""

        registry_id = id(registry)
        self._active_leases[registry_id] = self._active_leases.get(registry_id, 0) + 1

    async def release(self, registry: ModelRegistry) -> None:
        """Release a run lease and close an invalidated snapshot when idle."""

        registry_id = id(registry)
        leases = self._active_leases.get(registry_id, 0)
        if leases <= 1:
            self._active_leases.pop(registry_id, None)
            retired = self._retired_registries.pop(registry_id, None)
            if retired is not None:
                await retired.close()
            return
        self._active_leases[registry_id] = leases - 1

    async def _retire(self, registry: ModelRegistry) -> None:
        """Remove a snapshot from reuse without interrupting an active stream."""

        registry_id = id(registry)
        if self._active_leases.get(registry_id, 0) > 0:
            self._retired_registries[registry_id] = registry
            return
        await registry.close()

    async def invalidate(
        self,
        *,
        tenant_id: str | None = None,
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Evict exact or scoped snapshots after a committed config change."""

        targets = [
            key
            for key in self._cache
            if (tenant_id is None or key[0] == tenant_id)
            and (model_id is None or key[1] == model_id)
            and (provider_id is None or key[2] in {None, provider_id})
        ]
        for key in targets:
            _expires, registry = self._cache.pop(key)
            await self._retire(registry)

    async def resolve(
        self,
        tenant_id: str,
        model_id: str,
        provider_id: str | None = None,
    ) -> ModelRegistry | None:
        if self.database is None:
            return None

        exact_provider_id = None
        if provider_id is not None:
            exact_provider_id = str(provider_id)
            if not exact_provider_id or exact_provider_id != exact_provider_id.strip():
                return None

        cache_key = (tenant_id or "default", model_id, exact_provider_id)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(cache_key)
            return cached[1]
        if cached is not None:
            _expires, expired_registry = self._cache.pop(cache_key)
            await self._retire(expired_registry)

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(cache_key)
            now = time.monotonic()
            if cached is not None and cached[0] > now:
                self._cache.move_to_end(cache_key)
                return cached[1]
            registry = await self._resolve_uncached(
                tenant_id=tenant_id,
                model_id=model_id,
                exact_provider_id=exact_provider_id,
            )
            if registry is not None:
                self._cache[cache_key] = (now + self.cache_ttl_seconds, registry)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self.cache_max_entries:
                    _old_key, (_expires, old_registry) = self._cache.popitem(last=False)
                    await self._retire(old_registry)
            return registry

    async def close(self) -> None:
        """Close all cached and retired provider clients during process shutdown."""

        registries = {
            id(registry): registry for _expires, registry in self._cache.values()
        }
        registries.update(self._retired_registries)
        self._cache.clear()
        self._retired_registries.clear()
        self._active_leases.clear()
        for registry in registries.values():
            await registry.close()

    async def list_model_infos(self, tenant_id: str) -> list[Any]:
        """Return enabled tenant model profiles for the Assistant picker."""

        if self.database is None:
            return []
        try:
            rows = await self.database.fetch(
                """
                SELECT m.model_id, m.display_name, m.context_window,
                       m.max_output_tokens, m.supports_vision, m.supports_tools,
                       m.catalog_capabilities, m.capability_overrides,
                       m.capability_revision, m.input_price_per_1k,
                       m.output_price_per_1k, m.access_level,
                       p.provider_id, p.api_type, p.base_url
                FROM llm_models AS m
                JOIN llm_providers AS p
                  ON p.tenant_id = m.tenant_id
                 AND p.provider_id = m.provider_id
                WHERE m.tenant_id = $1
                  AND m.is_enabled = true
                  AND p.is_enabled = true
                ORDER BY m.sort_order DESC, m.display_name ASC
                """,
                tenant_id or "default",
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.tenant_model.list_failed", exc
            )
            return []
        normalized_rows = []
        for row in rows:
            data = dict(row)
            provider = _runtime_provider(data)
            if provider is not None:
                normalized_rows.append(
                    {
                        **data,
                        "capability_provider_id": data.get("provider_id"),
                        "provider_id": provider.value,
                    }
                )
        registry = ModelRegistry(use_default_models=False)
        registry.replace_models_from_database_rows(normalized_rows)
        # Do NOT filter by is_provider_configured here: this throwaway registry
        # intentionally carries no credentials (the listing SELECT omits keys),
        # so the filter would drop every model.
        return registry.list_loaded_models()

    async def _resolve_uncached(
        self,
        *,
        tenant_id: str,
        model_id: str,
        exact_provider_id: str | None,
    ) -> ModelRegistry | None:
        try:
            query = """
                SELECT m.model_id, m.display_name, m.context_window,
                       m.max_output_tokens, m.supports_vision, m.supports_tools,
                       m.catalog_capabilities, m.capability_overrides,
                       m.capability_revision,
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
                """
            arguments: tuple[str, ...] = (tenant_id or "default", model_id)
            if exact_provider_id is not None:
                query = query.replace(
                    "                  AND m.is_enabled = true",
                    "                  AND m.provider_id = $3\n"
                    "                  AND m.is_enabled = true",
                )
                arguments = (*arguments, exact_provider_id)
            row = await self.database.fetchrow(
                query,
                *arguments,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.models.tenant_registry.internal_failure", exc
            )
            return None

        if not row:
            return None
        data = dict(row)
        if exact_provider_id is not None and str(data.get("provider_id") or "") != exact_provider_id:
            logger.warning("Pinned Agent provider resolution returned a mismatched row")
            return None
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
            [
                {
                    **data,
                    "capability_provider_id": data.get("provider_id"),
                    "provider_id": provider.value,
                }
            ],
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
            record_internal_exception(
                __name__,
                "assistant.tenant_model.provider_configuration_invalid",
                exc,
            )
        return registry


__all__ = ["TenantModelRegistryResolver"]
