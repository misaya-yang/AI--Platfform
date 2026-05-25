from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.api.v1.proxy import _inject_langgraph_model_override_config
from src.proxy.config_loader import ProxyServiceConfig


class FakeProviderService:
    async def get_runtime_provider_config(self, tenant_id: str, provider_id: str) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert provider_id == "dashscope"
        return {
            "is_enabled": True,
            "runtime_provider": "dashscope",
            "runtime_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode",
            "api_key": "gateway-runtime-secret",
        }


class FakeModelService:
    async def get_provider_model(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        assert tenant_id == "tenant-a"
        assert provider_id == "dashscope"
        assert model_id == "qwen3.6-plus"
        return {"is_enabled": True}


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                provider_service=FakeProviderService(),
                model_service=FakeModelService(),
            )
        )
    )


def _config(model_override: dict[str, Any]) -> ProxyServiceConfig:
    return ProxyServiceConfig(
        service_id="local-2024-agent",
        service_name="Sheikh Wahda",
        upstream_url="http://imam-agent:8000",
        assistant_id="Imam",
        metadata={"adapter_type": "langgraph"},
        model_override=model_override,
    )


@pytest.mark.asyncio
async def test_proxy_run_injects_gateway_resolved_hejaz_model() -> None:
    body = json.dumps(
        {
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "config": {
                "configurable": {
                    "thread_id": "t1",
                    "hejaz_model": {"_api_key": "browser-secret"},
                }
            },
        }
    ).encode("utf-8")

    updated = await _inject_langgraph_model_override_config(
        request=_request(),
        body=body,
        method="POST",
        path="threads/t1/runs/stream",
        service_config=_config(
            {
                "enabled": True,
                "provider_id": "dashscope",
                "model_id": "qwen3.6-plus",
                "temperature": 0.2,
                "cache_epoch": 7,
            }
        ),
        tenant_id="tenant-a",
    )

    payload = json.loads((updated or b"{}").decode("utf-8"))
    hejaz_model = payload["config"]["configurable"]["hejaz_model"]
    assert hejaz_model["tenant_id"] == "tenant-a"
    assert hejaz_model["provider_id"] == "dashscope"
    assert hejaz_model["provider"] == "dashscope"
    assert hejaz_model["model_id"] == "qwen3.6-plus"
    assert hejaz_model["model"] == "qwen3.6-plus"
    assert hejaz_model["temperature"] == 0.2
    assert hejaz_model["cache_epoch"] == "7"
    assert hejaz_model["_api_key"] == "gateway-runtime-secret"
    assert hejaz_model["api_key_fingerprint"] == hashlib.sha256(
        b"gateway-runtime-secret"
    ).hexdigest()[:16]
    assert "browser-secret" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_proxy_run_scrubs_browser_hejaz_model_when_override_disabled() -> None:
    body = json.dumps(
        {
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "config": {"configurable": {"hejaz_model": {"_api_key": "browser-secret"}}},
        }
    ).encode("utf-8")

    updated = await _inject_langgraph_model_override_config(
        request=_request(),
        body=body,
        method="POST",
        path="runs/stream",
        service_config=_config({"enabled": False}),
        tenant_id="tenant-a",
    )

    payload = json.loads((updated or b"{}").decode("utf-8"))
    assert "hejaz_model" not in payload["config"]["configurable"]
    assert "browser-secret" not in json.dumps(payload)
