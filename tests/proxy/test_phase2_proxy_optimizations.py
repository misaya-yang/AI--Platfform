from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.proxy.config_loader import ProxyServiceConfig
from src.proxy.response_cache import ResponseCache
from src.proxy.transparent_proxy import TransparentProxy


def _service_config() -> ProxyServiceConfig:
    return ProxyServiceConfig(
        service_id="agent-1",
        service_name="agent",
        upstream_url="http://langgraph:8000",
        default_model="fallback-model",
        model_override={"provider_id": "dashscope"},
        metadata={"adapter_type": "langgraph"},
    )


def test_resolve_billing_hints_prefers_effective_model() -> None:
    config = _service_config()
    model, provider = TransparentProxy._resolve_billing_hints(
        {
            "config": {
                "configurable": {
                    "gateway_model": {"model_id": "ignored-if-effective-set"},
                }
            }
        },
        config=config,
        effective_model="qwen-max",
        effective_provider="dashscope-prod",
        is_run_operation=True,
    )
    assert model == "qwen-max"
    assert provider == "dashscope-prod"


def test_resolve_billing_hints_reads_redacted_gateway_model() -> None:
    config = _service_config()
    model, provider = TransparentProxy._resolve_billing_hints(
        {
            "config": {
                "configurable": {
                    "gateway_model": {
                        "model_id": "qwen-max",
                        "provider_id": "dashscope",
                    }
                }
            }
        },
        config=config,
        is_run_operation=True,
    )
    assert model == "qwen-max"
    assert provider == "dashscope"


def test_response_cache_uses_parsed_body_without_decoding_bytes() -> None:
    cache = ResponseCache(database=None)
    parsed = {
        "model": "ignored",
        "config": {"configurable": {"gateway_model": {"model_id": "qwen-max"}}},
    }
    stable, model = ResponseCache._normalize_body(b"{}", parsed_body=parsed)
    assert model == "qwen-max"
    assert "qwen-max" in stable


@pytest.mark.asyncio
async def test_availability_stale_while_revalidate_returns_cached_snapshot() -> None:
    proxy = TransparentProxy.__new__(TransparentProxy)
    proxy.availability_cache_ttl = 0.01
    proxy._availability = {
        "agent-1": {
            "availability_status": "available",
            "available_upstreams": ["http://langgraph:8000"],
            "last_health_check_at": 0.0,
            "last_health_error": None,
        }
    }
    proxy._availability_lock = asyncio.Lock()
    proxy._availability_refresh_inflight = {}

    refresh_calls = 0

    async def slow_refresh(_config: ProxyServiceConfig) -> dict:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0.05)
        return {
            "availability_status": "available",
            "available_upstreams": ["http://langgraph:8000"],
            "last_health_check_at": 1.0,
            "last_health_error": None,
        }

    proxy._refresh_service_availability = slow_refresh  # type: ignore[method-assign]

    config = _service_config()
    snapshot = await proxy.get_service_availability(config)
    assert snapshot["availability_status"] == "available"
    await asyncio.sleep(0.02)
    assert refresh_calls == 1