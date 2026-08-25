from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from ai_gateway_core.comm.client import InternalServiceHTTPError
from ai_gateway_core.knowledge import KnowledgeClientLike
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


@pytest.mark.asyncio
async def test_kb_proxy_client_does_not_disguise_dataset_outage_as_empty() -> None:
    client = KBProxyClient(
        base_url="http://knowledge-service.test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"detail": "unavailable"})
        ),
    )
    try:
        with pytest.raises(InternalServiceHTTPError):
            await client.list_datasets(SimpleNamespace(user_id="u1", tenant_id="t1", tier="normal"))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_kb_proxy_client_forwards_supported_retrieval_options() -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"results": [], "metadata": {}})

    client = KBProxyClient(
        base_url="http://knowledge-service.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        await client.retrieve(
            SimpleNamespace(user_id="u1", tenant_id="t1", tier="normal"),
            "dataset-1",
            "query",
            candidate_top_k=40,
            rerank=False,
            mmr=True,
            include_images=False,
            include_associated_images=False,
            metadata_filter={"madhab": "hanafi"},
            ignored_option="not-forwarded",
        )
    finally:
        await client.close()

    assert isinstance(client, KnowledgeClientLike)
    assert seen_payload == {
        "query": "query",
        "top_k": 5,
        "mode": "hybrid",
        "candidate_top_k": 40,
        "rerank": False,
        "mmr": True,
        "include_images": False,
        "include_associated_images": False,
        "metadata_filter": {"madhab": "hanafi"},
    }


@pytest.mark.asyncio
async def test_kb_proxy_client_multimodal_wrappers_are_explicit() -> None:
    seen_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "text": "diagram",
                        "score": 0.9,
                        "content_type": "image",
                        "image_url": "https://example.test/image.png",
                        "associated_images": [{"image_segment_id": "img-1"}],
                    }
                ],
                "metadata": {},
            },
        )

    client = KBProxyClient(
        base_url="http://knowledge-service.test",
        transport=httpx.MockTransport(handler),
    )

    try:
        results, _ = await client.retrieve_with_images(
            SimpleNamespace(user_id="u1", tenant_id="t1", tier="normal"),
            "dataset-1",
            "diagram",
        )
        await client.retrieve_with_images_v2(
            SimpleNamespace(user_id="u1", tenant_id="t1", tier="normal"),
            "dataset-1",
            "policy",
            intent="find_document",
        )
    finally:
        await client.close()

    assert results[0].content_type == "image"
    assert results[0].associated_images == ({"image_segment_id": "img-1"},)
    assert seen_payloads[0]["include_images"] is True
    assert seen_payloads[0]["include_associated_images"] is True
    assert seen_payloads[1]["include_images"] is False
    assert seen_payloads[1]["include_associated_images"] is False
