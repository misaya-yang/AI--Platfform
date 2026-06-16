from __future__ import annotations

import httpx
import pytest
from ai_gateway_core.comm.client import InternalServiceClient, InternalServiceClientConfig

from src.core.observability.metrics import get_metrics


@pytest.mark.asyncio
async def test_internal_service_client_records_low_cardinality_metrics() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service_name = "metrics-test-service"
    client = InternalServiceClient(
        InternalServiceClientConfig(
            name=service_name,
            base_url="http://metrics.test",
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.request_json("GET", "/health")
    finally:
        await client.close()

    metrics = get_metrics()
    counter = metrics._counters["service_call_total"]
    assert counter.get(service=service_name, method="GET", status="200") >= 1
    assert "path" not in counter.label_names
    assert metrics._gauges["service_call_inflight"].get(service=service_name) == 0
