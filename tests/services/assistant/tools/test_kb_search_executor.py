from types import SimpleNamespace

import pytest
from assistant_service.core.tools.builtin_tools import KBSearchExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "expected_include_images"),
    [("general", False), ("find_document", False), ("find_image", True)],
)
async def test_kb_search_only_enables_images_for_image_intent(
    intent: str,
    expected_include_images: bool,
):
    calls = []

    class FakeKB:
        async def retrieve_with_images_v2(self, **kwargs):
            calls.append(kwargs)
            return [], {}

    result = await KBSearchExecutor(FakeKB()).execute(
        ToolCallRequest(
            call_id="call-1",
            tool_name="search_knowledge_base",
            arguments={
                "query": "architecture",
                "intent": intent,
                "dataset_ids": ["dataset-1"],
            },
            user=SimpleNamespace(user_id="u1", tenant_id="t1"),
        )
    )

    assert result.success is True
    assert calls[0]["include_images"] is expected_include_images
    assert calls[0]["vlm_rerank"] is expected_include_images


@pytest.mark.asyncio
async def test_kb_search_merges_datasets_by_rank_not_local_score():
    def result(segment_id: str, score: float):
        return SimpleNamespace(
            segment_id=segment_id,
            document_id=f"doc-{segment_id}",
            text=segment_id,
            score=score,
            metadata={},
            image_url=None,
        )

    class FakeKB:
        async def retrieve_with_images_v2(self, dataset_id, **_kwargs):
            if dataset_id == "dataset-a":
                return [result("a-rank-1", 0.99), result("a-rank-2", 0.98)], {}
            return [result("b-rank-1", 0.1)], {}

    response = await KBSearchExecutor(FakeKB()).execute(
        ToolCallRequest(
            call_id="call-1",
            tool_name="search_knowledge_base",
            arguments={
                "query": "architecture",
                "dataset_ids": ["dataset-a", "dataset-b"],
                "top_k": 3,
            },
            user=SimpleNamespace(user_id="u1", tenant_id="t1"),
        )
    )

    output = str(response.result)
    assert response.success is True
    assert output.index("a-rank-1") < output.index("b-rank-1") < output.index("a-rank-2")
