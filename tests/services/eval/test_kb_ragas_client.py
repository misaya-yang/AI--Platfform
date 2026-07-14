from __future__ import annotations

import json

import httpx
import pytest

from src.services.eval.kb_ragas_client import KbRagasClient

_VALID_METRIC_RESULT = {
    "metric": "context_relevancy",
    "score": 0.8,
    "explanation": "Relevant.",
    "label": "pass",
}


@pytest.mark.asyncio
async def test_kb_ragas_client_parses_metric_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/eval/ragas"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["query"] == "refund policy"
        assert payload["contexts"] == ["chunk one"]
        assert payload["answer"] == "Refunds are available for 30 days."
        return httpx.Response(
            200,
            json={
                "judge_model": "qwen-test",
                "results": [
                    {
                        "metric": "context_relevancy",
                        "score": 0.0,
                        "explanation": "Judge unavailable.",
                        "label": "review",
                        "failure_kind": "infrastructure",
                    }
                ],
            },
        )

    from ai_gateway_core.comm.client import InternalServiceClient, InternalServiceClientConfig

    client = KbRagasClient(base_url="http://kb.test", timeout_s=5.0)
    client._service_client = InternalServiceClient(  # noqa: SLF001 - test setup
        InternalServiceClientConfig(name="knowledge-service", base_url="http://kb.test"),
        transport=httpx.MockTransport(handler),
    )

    results = await client.evaluate_retrieval(
        query="refund policy",
        answer="Refunds are available for 30 days.",
        contexts=["chunk one"],
        metrics=["context_relevancy"],
    )

    assert len(results) == 1
    assert results[0].metric == "context_relevancy"
    assert results[0].score == 0.0
    assert results[0].judge_model == "qwen-test"
    assert results[0].failure_kind == "infrastructure"
    await client.close()


@pytest.mark.asyncio
async def test_kb_ragas_client_uses_knowledge_shared_secret_fallback(monkeypatch) -> None:
    import src.services.eval.kb_ragas_client as kb_ragas_client_module

    monkeypatch.delenv("GATEWAY_ASSISTANT_SHARED_SECRET", raising=False)
    monkeypatch.setenv("GATEWAY_KNOWLEDGE_SHARED_SECRET", "knowledge-secret")
    kb_ragas_client_module._gateway_secret_signer = None

    signer = kb_ragas_client_module._get_signer()
    assert signer is not None
    assert signer.secret == "knowledge-secret"


@pytest.mark.asyncio
async def test_kb_ragas_client_raises_on_http_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = KbRagasClient(base_url="http://kb.test", timeout_s=5.0)
    from ai_gateway_core.comm.client import InternalServiceClient, InternalServiceClientConfig

    client._service_client = InternalServiceClient(  # noqa: SLF001 - test setup
        InternalServiceClientConfig(name="knowledge-service", base_url="http://kb.test"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await client.evaluate_retrieval(query="q", contexts=["c"])
    await client.close()


@pytest.mark.parametrize(
    "raw_results",
    [
        pytest.param([_VALID_METRIC_RESULT, "bad"], id="mixed-valid-and-non-object"),
        pytest.param([{**_VALID_METRIC_RESULT, "metric": " "}], id="blank-metric"),
        pytest.param([_VALID_METRIC_RESULT, dict(_VALID_METRIC_RESULT)], id="duplicate-metric"),
        pytest.param("not-a-list", id="non-list"),
        pytest.param([], id="empty-list"),
    ],
)
@pytest.mark.asyncio
async def test_kb_ragas_client_rejects_malformed_result_collection(
    raw_results: object,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"judge_model": "qwen-test", "results": raw_results},
        )

    from ai_gateway_core.comm.client import InternalServiceClient, InternalServiceClientConfig

    client = KbRagasClient(base_url="http://kb.test", timeout_s=5.0)
    client._service_client = InternalServiceClient(  # noqa: SLF001 - test setup
        InternalServiceClientConfig(name="knowledge-service", base_url="http://kb.test"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError) as exc_info:
        await client.evaluate_retrieval(query="q", contexts=["c"])

    assert str(exc_info.value) == "knowledge-service RAGAS eval returned invalid results"
    await client.close()
