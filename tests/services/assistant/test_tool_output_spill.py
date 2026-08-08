from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.artifact_persister import persist_and_collect_events
from assistant_service.core.agent.middlewares.response_cap import ResponseCapMiddleware
from assistant_service.core.agent.middlewares.tool_output_spill import (
    ToolOutputSpillMiddleware,
)
from assistant_service.core.agent.tool_result_formatter import compact_tool_result_for_model
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
)


class _Storage:
    def __init__(self, *, fail_url: bool = False) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.fail_url = fail_url

    async def create_artifact(self, **values: Any) -> Any:
        self.created.append(values)
        return SimpleNamespace(artifact_id="art-spill-1")

    async def get_presigned_download_url(self, _artifact: Any) -> str:
        if self.fail_url:
            raise RuntimeError("url unavailable")
        return "file:///private/tool-output.txt"

    async def delete_artifact(self, artifact_id: str) -> bool:
        self.deleted.append(artifact_id)
        return True


def _definition(**metadata: Any) -> ToolDefinition:
    return ToolDefinition(
        name="large_read",
        description="Read a large public report",
        parameters=[ToolParameter(name="query", type="string", description="Query")],
        risk_level=ToolRiskLevel.LOW,
        capability_metadata={
            "persist_large_output": True,
            "output_sensitivity": "non_sensitive",
            "operation_kind": "read",
            **metadata,
        },
    )


def _ctx() -> Any:
    return SimpleNamespace(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        run_id="run-1",
    )


@pytest.mark.asyncio
async def test_opted_in_large_text_is_redacted_scoped_and_reused_as_artifact() -> None:
    storage = _Storage()
    definition = _definition()
    middleware = ToolOutputSpillMiddleware(
        artifact_storage=storage,
        definition_resolver=lambda _ctx, _name: definition,
        enabled=True,
        threshold_chars=4_000,
    )
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    payload = ("public report line\n" * 300) + f"api_key={secret}"
    result = ToolCallResult(
        call_id="call-1",
        tool_name="large_read",
        success=True,
        result=payload,
    )

    spilled = await middleware.on_tool_result(_ctx(), "large_read", {}, result)

    assert spilled is not None
    assert storage.created[0]["tenant_id"] == "tenant-1"
    assert storage.created[0]["user_id"] == "user-1"
    assert storage.created[0]["session_id"] == "session-1"
    stored = storage.created[0]["content"].decode("utf-8")
    assert secret not in stored
    assert "[redacted]" in stored
    receipt = spilled.metadata["tool_output_artifact"]
    assert receipt["artifact_id"] == "art-spill-1"
    assert spilled.output_files[0]["content_base64"] == ""

    capped = await ResponseCapMiddleware(max_tokens=1_000).on_tool_result(
        _ctx(), "large_read", {}, spilled
    )
    model_text = compact_tool_result_for_model(
        "large_read",
        capped.result,
        capped.metadata,
    )
    assert len(capped.result) < len(payload)
    assert "art-spill-1" in model_text
    assert "/api/v1/assistant/artifacts/art-spill-1/download" in model_text

    persisted, events, ids = await persist_and_collect_events(
        artifact_storage=storage,
        user=SimpleNamespace(tenant_id="tenant-1", user_id="user-1"),
        session_id="session-1",
        tool_name="large_read",
        tool_output_files=spilled.output_files,
    )
    assert len(storage.created) == 1
    assert persisted[0]["artifact_id"] == "art-spill-1"
    assert events[0]["download_url"] == "/api/v1/assistant/artifacts/art-spill-1/download"
    assert ids == ["art-spill-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definition",
    [
        _definition(persist_large_output=False),
        _definition(output_sensitivity="sensitive"),
        _definition(operation_kind="unknown"),
        ToolDefinition(
            name="large_read",
            description="High risk",
            parameters=[],
            risk_level=ToolRiskLevel.HIGH,
            capability_metadata={
                "persist_large_output": True,
                "output_sensitivity": "non_sensitive",
                "operation_kind": "read",
            },
        ),
    ],
)
async def test_sensitive_unknown_or_high_risk_output_never_spills(
    definition: ToolDefinition,
) -> None:
    storage = _Storage()
    middleware = ToolOutputSpillMiddleware(
        artifact_storage=storage,
        definition_resolver=lambda _ctx, _name: definition,
        enabled=True,
        threshold_chars=4_000,
    )
    result = ToolCallResult(
        call_id="call-1",
        tool_name="large_read",
        success=True,
        result="x" * 5_000,
    )

    assert await middleware.on_tool_result(_ctx(), "large_read", {}, result) is None
    assert storage.created == []


@pytest.mark.asyncio
async def test_disabled_spill_keeps_existing_truncation_only_behavior() -> None:
    storage = _Storage()
    middleware = ToolOutputSpillMiddleware(
        artifact_storage=storage,
        definition_resolver=lambda _ctx, _name: _definition(),
        enabled=False,
        threshold_chars=4_000,
    )
    result = ToolCallResult(
        call_id="call-1",
        tool_name="large_read",
        success=True,
        result="x" * 5_000,
    )

    assert await middleware.on_tool_result(_ctx(), "large_read", {}, result) is None
    assert storage.created == []


@pytest.mark.asyncio
async def test_incomplete_persistence_is_rolled_back() -> None:
    storage = _Storage(fail_url=True)
    middleware = ToolOutputSpillMiddleware(
        artifact_storage=storage,
        definition_resolver=lambda _ctx, _name: _definition(),
        enabled=True,
        threshold_chars=4_000,
    )
    result = ToolCallResult(
        call_id="call-1",
        tool_name="large_read",
        success=True,
        result="x" * 5_000,
    )

    assert await middleware.on_tool_result(_ctx(), "large_read", {}, result) is None
    assert storage.deleted == ["art-spill-1"]
