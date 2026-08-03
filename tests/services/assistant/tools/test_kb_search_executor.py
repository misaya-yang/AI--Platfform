import logging
from types import SimpleNamespace

import pytest
from assistant_service.core.tools.builtin_tools import KB_SEARCH_DEFINITION, KBSearchExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent",
    ["general", "find_document"],
)
async def test_kb_search_is_text_only(intent: str):
    calls = []

    class FakeKB:
        async def retrieve(self, **kwargs):
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
    assert calls[0]["include_images"] is False


@pytest.mark.asyncio
async def test_kb_search_rejects_image_intent_before_service_call():
    calls = []

    class FakeKB:
        async def retrieve(self, **kwargs):
            calls.append(kwargs)
            return [], {}

    result = await KBSearchExecutor(FakeKB()).execute(
        ToolCallRequest(
            call_id="call-image",
            tool_name="search_knowledge_base",
            arguments={
                "query": "architecture",
                "intent": "find_image",
                "dataset_ids": ["dataset-1"],
            },
            user=SimpleNamespace(user_id="u1", tenant_id="t1"),
        )
    )

    assert result.success is False
    assert result.error == "Unsupported knowledge retrieval intent"
    assert calls == []


def test_kb_search_schema_advertises_bounded_text_only_contract():
    parameters = KB_SEARCH_DEFINITION.to_openai_schema()["function"]["parameters"]["properties"]

    assert parameters["intent"]["enum"] == ["general", "find_document"]
    assert parameters["query"]["maxLength"] == 4096
    assert parameters["dataset_ids"]["maxItems"] == 8
    assert parameters["dataset_ids"]["uniqueItems"] is True
    assert parameters["top_k"] == {
        "type": "integer",
        "description": "Number of results to return (1-20). Default is 5.",
        "minimum": 1,
        "maximum": 20,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "x" * 4097, "dataset_ids": ["dataset-1"]},
        {"query": "architecture", "dataset_ids": [f"dataset-{i}" for i in range(9)]},
        {"query": "architecture", "dataset_ids": ["dataset-1", "dataset-1"]},
        {"query": "architecture", "dataset_ids": ["dataset-1"], "top_k": 21},
        {
            "query": "architecture",
            "dataset_ids": ["dataset-1"],
            "score_threshold": 1.1,
        },
    ],
)
async def test_kb_search_rejects_amplifying_arguments_before_service_call(arguments):
    calls = []

    class FakeKB:
        async def retrieve(self, **kwargs):
            calls.append(kwargs)
            return [], {}

    result = await KBSearchExecutor(FakeKB()).execute(
        ToolCallRequest(
            call_id="call-invalid",
            tool_name="search_knowledge_base",
            arguments=arguments,
            user=SimpleNamespace(user_id="u1", tenant_id="t1"),
        )
    )

    assert result.success is False
    assert calls == []


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
        async def retrieve(self, dataset_id, **_kwargs):
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


@pytest.mark.asyncio
async def test_kb_search_failure_log_and_public_error_omit_sensitive_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-kb-api-secret"

    class FailingKB:
        @property
        def settings(self):
            raise RuntimeError(f"api_key={secret} " + "x" * 500)

    with caplog.at_level(
        logging.ERROR,
        logger="assistant_service.core.tools.builtin_tools",
    ):
        result = await KBSearchExecutor(FailingKB()).execute(
            ToolCallRequest(
                call_id="call-private-error",
                tool_name="search_knowledge_base",
                arguments={"query": "architecture", "dataset_ids": ["dataset-1"]},
                user=SimpleNamespace(user_id="u1", tenant_id="t1"),
            )
        )

    assert result.success is False
    assert result.error is not None
    assert len(result.error) <= 200
    assert secret not in result.error
    assert "api_key=[redacted]" in result.error
    assert secret not in caplog.text
    assert "assistant.kb_search_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_dataset_failure_metadata_and_log_omit_sensitive_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "synthetic-dataset-password"

    class FailingKB:
        async def retrieve(self, **_kwargs):
            raise RuntimeError(f"password={secret} " + "y" * 500)

    with caplog.at_level(
        logging.WARNING,
        logger="assistant_service.core.tools.builtin_tools",
    ):
        result = await KBSearchExecutor(FailingKB()).execute(
            ToolCallRequest(
                call_id="call-private-dataset-error",
                tool_name="search_knowledge_base",
                arguments={"query": "architecture", "dataset_ids": ["dataset-1"]},
                user=SimpleNamespace(user_id="u1", tenant_id="t1"),
            )
        )

    public_payload = str(result.to_dict())
    dataset_error = result.metadata["dataset_errors"]["dataset-1"]
    assert result.success is False
    assert result.error == "KB_SEARCH_FAILED"
    assert len(dataset_error) <= 200
    assert secret not in public_payload
    assert "password=[redacted]" in public_payload
    assert secret not in caplog.text
    assert "assistant.kb_dataset_search_failed" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
