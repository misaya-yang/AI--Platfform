import pytest
import httpx

from src.services.knowledge.text_reranker import (
    AsyncTextReranker,
    BGEReranker,
    _provider_failure_state,
    create_reranker,
    normalize_rerank_model,
    normalize_rerank_provider,
)


def test_normalize_provider_infers_bge_from_model():
    assert normalize_rerank_provider(None, "bge-reranker-v2-m3") == "bge"
    assert normalize_rerank_provider("", "BAAI/bge-reranker-v2-m3") == "bge"


def test_normalize_dashscope_model_alias():
    assert normalize_rerank_model("dashscope", "gte-rerank") == "gte-rerank-v2"
    assert normalize_rerank_model("dashscope", None) == "gte-rerank-v2"


def test_create_reranker_uses_normalized_dashscope_model():
    reranker = create_reranker(provider="dashscope", api_key="test-key", model="gte-rerank")
    assert isinstance(reranker, AsyncTextReranker)
    assert reranker.model == "gte-rerank-v2"


def test_create_reranker_infers_provider_from_model():
    reranker = create_reranker(provider="unknown-provider", model="bge-reranker-v2-m3")
    assert isinstance(reranker, BGEReranker)
    assert reranker.model_name == "BAAI/bge-reranker-v2-m3"


def test_create_reranker_requires_api_key_for_dashscope():
    with pytest.raises(ValueError):
        create_reranker(provider="dashscope", model="gte-rerank-v2")


@pytest.mark.asyncio
async def test_dashscope_reranker_opens_failure_circuit_on_permanent_error(monkeypatch):
    _provider_failure_state.clear()
    call_count = 0

    class _FailingClient:
        async def post(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            request = httpx.Request("POST", "https://dashscope.aliyuncs.com/test")
            return httpx.Response(
                400,
                request=request,
                text='{"code":"Arrearage","message":"Access denied"}',
            )

    async def _fake_get_http_client():
        return _FailingClient()

    monkeypatch.setattr(
        "src.services.knowledge.text_reranker._get_http_client",
        _fake_get_http_client,
    )

    reranker = AsyncTextReranker(api_key="test-key", model="gte-rerank-v2")

    with pytest.raises(httpx.HTTPStatusError):
        await reranker.rerank("ramadan rules", ["doc1", "doc2"])

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await reranker.rerank("ramadan rules", ["doc1", "doc2"])

    assert call_count == 1
