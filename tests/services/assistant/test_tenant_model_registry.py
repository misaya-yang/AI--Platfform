from __future__ import annotations

import asyncio
import time

from ai_gateway_core.enums import ModelProvider
from ai_gateway_core.security import encrypt_value
from assistant_service.core.models.tenant_registry import TenantModelRegistryResolver


class _Database:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


def _row(**overrides):
    row = {
        "model_id": "qwen3.7-plus",
        "display_name": "Qwen 3.7 Plus",
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "supports_vision": False,
        "supports_tools": True,
        "input_price_per_1k": 0,
        "output_price_per_1k": 0,
        "access_level": "public",
        "provider_id": "dashscope-intl",
        "api_type": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode",
        "api_key_encrypted": "",
        "metadata": {},
    }
    row.update(overrides)
    return row


async def test_ui_provider_secret_builds_isolated_runtime_registry():
    encryption_key = "e" * 64
    database = _Database(_row(api_key_encrypted=encrypt_value("provider-secret", encryption_key)))

    registry = await TenantModelRegistryResolver(
        database,
        encryption_key=encryption_key,
    ).resolve("tenant-a", "qwen3.7-plus")

    assert registry is not None
    model = registry.get_model("qwen3.7-plus")
    assert model is not None
    assert model.provider is ModelProvider.DASHSCOPE
    assert registry.is_provider_configured(ModelProvider.DASHSCOPE)
    assert registry._configs[ModelProvider.DASHSCOPE].api_key == "provider-secret"
    assert registry._configs[ModelProvider.DASHSCOPE].wire_protocol == "responses_v1"
    assert database.calls[0][1] == ("tenant-a", "qwen3.7-plus")
    await registry.close()


async def test_explicit_tenant_chat_compatibility_fallback_is_preserved():
    encryption_key = "e" * 64
    database = _Database(
        _row(
            api_key_encrypted=encrypt_value("provider-secret", encryption_key),
            metadata={"wire_protocol": "chat_completions"},
        )
    )

    registry = await TenantModelRegistryResolver(
        database,
        encryption_key=encryption_key,
    ).resolve("tenant-a", "qwen3.7-plus")

    assert registry is not None
    assert registry._configs[ModelProvider.DASHSCOPE].wire_protocol == "chat_completions"
    await registry.close()


async def test_agent_runtime_provider_pin_adds_exact_sql_filter():
    encryption_key = "e" * 64
    database = _Database(_row(api_key_encrypted=encrypt_value("provider-secret", encryption_key)))

    registry = await TenantModelRegistryResolver(
        database,
        encryption_key=encryption_key,
    ).resolve("tenant-a", "qwen3.7-plus", provider_id="dashscope-intl")

    assert registry is not None
    query, args = database.calls[0]
    assert "AND m.provider_id = $3" in query
    assert args == ("tenant-a", "qwen3.7-plus", "dashscope-intl")
    await registry.close()


async def test_agent_runtime_provider_pin_rejects_mismatched_database_row():
    encryption_key = "e" * 64
    database = _Database(
        _row(
            provider_id="dashscope-cn",
            api_key_encrypted=encrypt_value("wrong-provider-secret", encryption_key),
        )
    )

    registry = await TenantModelRegistryResolver(
        database,
        encryption_key=encryption_key,
    ).resolve("tenant-a", "qwen3.7-plus", provider_id="dashscope-intl")

    assert registry is None


async def test_env_config_remains_the_fallback_when_db_has_no_secret():
    registry = await TenantModelRegistryResolver(_Database(_row())).resolve(
        "tenant-a",
        "qwen3.7-plus",
    )

    assert registry is None


async def test_wrong_encryption_key_never_forwards_ciphertext_as_api_key():
    database = _Database(_row(api_key_encrypted=encrypt_value("provider-secret", "correct-key")))

    registry = await TenantModelRegistryResolver(
        database,
        encryption_key="wrong-key",
    ).resolve("tenant-a", "qwen3.7-plus")

    assert registry is not None
    assert registry.get_model("qwen3.7-plus") is not None
    assert not registry.is_provider_configured(ModelProvider.DASHSCOPE)
    await registry.close()


async def test_unknown_provider_fails_closed_without_default_catalog():
    database = _Database(
        _row(
            provider_id="unsupported",
            api_type="unsupported",
            base_url="https://unsupported.invalid",
            api_key_encrypted="plaintext-secret",
        )
    )

    registry = await TenantModelRegistryResolver(database).resolve(
        "tenant-a",
        "qwen3.7-plus",
    )

    assert registry is not None
    assert registry.get_available_models() == []
    await registry.close()


async def test_concurrent_cold_resolves_use_one_database_query():
    encryption_key = "e" * 64
    database = _Database(_row(api_key_encrypted=encrypt_value("provider-secret", encryption_key)))
    resolver = TenantModelRegistryResolver(database, encryption_key=encryption_key)

    registries = await asyncio.gather(
        *(resolver.resolve("tenant-a", "qwen3.7-plus") for _ in range(100))
    )

    assert len(database.calls) == 1
    assert all(registry is registries[0] for registry in registries)
    await resolver.invalidate(tenant_id="tenant-a", model_id="qwen3.7-plus")


async def test_invalidation_does_not_close_an_active_run_snapshot():
    class _Registry:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    resolver = TenantModelRegistryResolver(_Database(None))
    registry = _Registry()
    resolver._cache[("tenant-a", "model-a", None)] = (
        time.monotonic() + 30,
        registry,
    )
    resolver.retain(registry)

    await resolver.invalidate(tenant_id="tenant-a", model_id="model-a")

    assert registry.close_calls == 0
    await resolver.release(registry)
    assert registry.close_calls == 1


class _ListDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


def _info_row(**overrides):
    row = {
        "model_id": "qwen3.7-plus",
        "display_name": "Qwen 3.7 Plus",
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "supports_vision": False,
        "supports_tools": True,
        "catalog_capabilities": {},
        "capability_overrides": {},
        "capability_revision": 4,
        "input_price_per_1k": 0,
        "output_price_per_1k": 0,
        "access_level": "public",
        "provider_id": "dashscope-intl",
        "api_type": "openai",
        "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode",
    }
    row.update(overrides)
    return row


async def test_list_model_infos_returns_rows_with_runtime_provider_mapping():
    database = _ListDatabase(
        [
            _info_row(),
            # Unknown runtime provider: the row is skipped, not surfaced
            # half-configured to the picker.
            _info_row(
                model_id="mystery-model",
                provider_id="mystery",
                api_type="carrier-pigeon",
                base_url="",
            ),
        ]
    )
    resolver = TenantModelRegistryResolver(database)

    infos = await resolver.list_model_infos("tenant-a")

    # ModelInfo objects, not raw rows: the picker consumes the merged
    # capability profile, and unknown-provider rows never reach it.
    assert [info.id for info in infos] == ["qwen3.7-plus"]
    info = infos[0]
    assert info.provider is ModelProvider.DASHSCOPE
    assert info.capability_revision == 4
    assert info.capability_profile is not None
    assert info.supports_tools is True
    query, args = database.calls[0]
    assert "m.is_enabled = true" in query
    assert "p.is_enabled = true" in query
    assert args == ("tenant-a",)


async def test_list_model_infos_is_fail_soft_and_handles_absent_database():
    class _BrokenDatabase:
        async def fetch(self, query, *args):
            del query, args
            raise RuntimeError("database offline")

    assert await TenantModelRegistryResolver(_BrokenDatabase()).list_model_infos("t") == []
    assert await TenantModelRegistryResolver(None).list_model_infos("t") == []
