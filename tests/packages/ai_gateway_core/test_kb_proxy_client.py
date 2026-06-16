from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from ai_gateway_core.knowledge.proxy_client import KBProxyClient
from ai_gateway_core.proxy.request_id_middleware import REQUEST_ID_CTX


@pytest.mark.asyncio
async def test_kb_proxy_client_propagates_request_id_to_knowledge_service() -> None:
    seen_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"datasets": []})

    client = KBProxyClient(
        base_url="http://knowledge-service.test",
        transport=httpx.MockTransport(handler),
    )

    token = REQUEST_ID_CTX.set("req-kb-hop-123")
    try:
        await client.list_datasets(SimpleNamespace(user_id="u1", tenant_id="t1", tier="normal"))
    finally:
        REQUEST_ID_CTX.reset(token)
        await client.close()

    assert seen_headers["x-request-id"] == "req-kb-hop-123"


def test_kb_proxy_client_accepts_custom_timeout_and_limits() -> None:
    timeout = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    limits = httpx.Limits(max_connections=7, max_keepalive_connections=3)

    client = KBProxyClient(
        base_url="http://knowledge-service.test",
        timeout=timeout,
        limits=limits,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    http_client = client._get_client()

    assert http_client.timeout == timeout


def test_kb_proxy_client_reads_timeout_and_pool_limits_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_PROXY_CONNECT_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("KB_PROXY_READ_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("KB_PROXY_WRITE_TIMEOUT_SECONDS", "6")
    monkeypatch.setenv("KB_PROXY_POOL_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("KB_PROXY_MAX_CONNECTIONS", "25")
    monkeypatch.setenv("KB_PROXY_MAX_KEEPALIVE_CONNECTIONS", "9")

    client = KBProxyClient(base_url="http://knowledge-service.test")

    assert client.timeout.connect == 1.5
    assert client.timeout.read == 45.0
    assert client.timeout.write == 6.0
    assert client.timeout.pool == 8.0
    assert client.limits == httpx.Limits(max_connections=25, max_keepalive_connections=9)
