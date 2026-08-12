from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.subagent_manager import SubAgentManager
from assistant_service.core.agent.subagent_types import SubAgentConfig, SubAgentType
from assistant_service.core.tool_invoker import RegistryToolInvoker, ToolInvocationContext
from assistant_service.core.tools.tool_artifact_reader import (
    READ_TOOL_ARTIFACT_DEFINITION,
    ReadToolArtifactExecutor,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolRegistry,
    ToolRiskLevel,
)


class _EvidenceCheckingModel:
    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self.calls = 0
        self.observed_evidence = ""

    async def chat_stream(self, **values: Any):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "sec-filing-call",
                        "type": "function",
                        "function": {
                            "name": "read_sec_filing",
                            "arguments": "{}",
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
            return

        tool_message = next(
            message for message in reversed(values["messages"]) if message["role"] == "tool"
        )
        self.observed_evidence = str(tool_message["content"])
        yield SimpleNamespace(
            content="The complete filing evidence, including its terminal citation, was reviewed.",
            tool_calls=[],
            finish_reason="stop",
        )


class _Storage:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.artifact_id = "art_seccomplete1"

    async def create_artifact(self, **values: Any) -> Any:
        self.created.append(values)
        return SimpleNamespace(artifact_id=self.artifact_id)

    async def get_presigned_download_url(self, _artifact: Any) -> str:
        return "file:///private/sec-evidence.txt"

    async def delete_artifact(self, _artifact_id: str) -> bool:
        return True

    async def read_artifact_scoped(self, artifact_id: str, *, max_bytes: int, **scope: str):
        assert max_bytes == 2_000_000
        if artifact_id != self.artifact_id or scope != {
            "tenant_id": "tenant-a",
            "session_id": "parent-session",
            "user_id": "parent-user",
        }:
            return None
        created = self.created[0]
        return (
            SimpleNamespace(
                artifact_id=artifact_id,
                source="tool_output_spill",
                turn_id=created["turn_id"],
                metadata=created["metadata"],
            ),
            created["content"],
        )

    def supports_scoped_artifact_reads(self) -> bool:
        return True


class _ArtifactRereadModel:
    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self.calls = 0
        self.receipt = ""
        self.reread = ""

    async def chat_stream(self, **values: Any):
        self.calls += 1
        if self.calls == 1:
            yield SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "sec-source",
                        "type": "function",
                        "function": {"name": "read_sec_filing", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
            )
            return
        tool_message = next(
            message for message in reversed(values["messages"]) if message["role"] == "tool"
        )
        if self.calls == 2:
            self.receipt = str(tool_message["content"])
            total_marker = "total_chars="
            total_start = self.receipt.index(total_marker) + len(total_marker)
            total_end = self.receipt.index(",", total_start)
            total_chars = int(self.receipt[total_start:total_end])
            artifact_marker = "artifact_id="
            artifact_start = self.receipt.index(artifact_marker) + len(artifact_marker)
            artifact_end = self.receipt.index(",", artifact_start)
            artifact_id = self.receipt[artifact_start:artifact_end]
            yield SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "sec-tail",
                        "type": "function",
                        "function": {
                            "name": "read_tool_artifact",
                            "arguments": json.dumps(
                                {
                                    "artifact_id": artifact_id,
                                    "offset": max(0, total_chars - 256),
                                    "limit": 256,
                                }
                            ),
                        },
                    }
                ],
                finish_reason="tool_calls",
            )
            return
        self.reread = str(tool_message["content"])
        yield SimpleNamespace(
            content="The SEC tail citation was independently reread from the verified artifact.",
            tool_calls=[],
            finish_reason="stop",
        )


def _parent_context() -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="parent-session",
        user_id="parent-user",
        tenant_id="tenant-a",
        request_id="parent-request",
        run_id="parent-run",
        user=SimpleNamespace(
            user_id="parent-user",
            is_authenticated=True,
            roles=[],
            tier="normal",
        ),
    )


async def _run_subagent(
    evidence: str,
    *,
    metadata: dict[str, Any] | None = None,
    artifact_storage: Any | None = None,
    spill_enabled: bool | None = None,
) -> tuple[_EvidenceCheckingModel, list[dict[str, Any]]]:
    registry = ToolRegistry()

    async def read_sec_filing(request: Any) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=evidence,
            metadata=dict(metadata or {}),
        )

    definition = ToolDefinition(
        name="read_sec_filing",
        description="Read the complete evidence for one SEC filing section",
        parameters=[],
        category=ToolCategory.RETRIEVAL,
        risk_level=ToolRiskLevel.LOW,
    )
    definition.capability_metadata = {
        "operation_kind": "read",
        "output_sensitivity": "non_sensitive",
    }
    registry.register(definition, read_sec_filing)
    model = _EvidenceCheckingModel()
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=registry,
        tool_invoker=RegistryToolInvoker(tool_registry=registry),
        artifact_storage=artifact_storage,
        tool_output_spill_enabled=spill_enabled,
    )
    parent = _parent_context()
    events = [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.TASK,
                prompt="Analyze the filing and retain the terminal citation.",
            ),
            parent_user=parent.user,
            parent_tenant_id=parent.tenant_id,
            parent_invocation_context=parent,
        )
    ]
    return model, events


@pytest.mark.asyncio
async def test_subagent_preserves_sec_evidence_beyond_2000_characters() -> None:
    terminal_citation = "SEC-END-CITATION: Item-1A-page-147"
    evidence = "Item 1A. Risk Factors\n" + ("Material liquidity risk disclosure. " * 90)
    evidence += terminal_citation
    assert len(evidence) > 2_000

    model, events = await _run_subagent(evidence)

    finished = next(event for event in events if event["event_type"] == "subagent_finished")
    assert finished["data"]["status"] == "completed"
    assert model.observed_evidence == evidence
    assert terminal_citation in model.observed_evidence


@pytest.mark.asyncio
async def test_subagent_rejects_evidence_that_exceeds_the_shared_inline_cap() -> None:
    evidence = "10-K consolidated cash-flow evidence\n" + ("0.20 percent; " * 7_500)
    evidence += "SEC-END-CITATION: Exhibit-99-page-212"
    assert len(evidence) > 100_000

    model, events = await _run_subagent(
        evidence,
        metadata={
            "response_cap_applied": False,
            "response_cap_max_tokens": 999_999,
        },
        spill_enabled=True,
    )

    tool_result = next(event for event in events if event["event_type"] == "subagent_tool_result")
    finished = next(event for event in events if event["event_type"] == "subagent_finished")
    assert tool_result["data"]["success"] is False
    assert finished["data"]["status"] == "failed"
    assert "INCOMPLETE_TOOL_OUTPUT" in model.observed_evidence
    assert evidence[:200] not in model.observed_evidence


@pytest.mark.asyncio
async def test_subagent_spills_oversized_safe_read_and_keeps_tail_receipt() -> None:
    storage = _Storage()
    terminal_citation = "SEC-END-CITATION: Exhibit-99-page-212"
    evidence = "10-K consolidated cash-flow evidence\n" + ("0.20 percent; " * 7_500)
    evidence += terminal_citation
    assert len(evidence) > 100_000

    model, events = await _run_subagent(
        evidence,
        artifact_storage=storage,
        spill_enabled=True,
    )

    tool_result = next(event for event in events if event["event_type"] == "subagent_tool_result")
    finished = next(event for event in events if event["event_type"] == "subagent_finished")
    assert tool_result["data"]["success"] is True
    assert finished["data"]["status"] == "completed"
    assert "COMPLETE_REDACTED_ARTIFACT_RECEIPT" in model.observed_evidence
    assert "art_seccomplete1" in model.observed_evidence
    assert terminal_citation in model.observed_evidence
    assert storage.created[0]["content"].decode("utf-8").endswith(terminal_citation)


@pytest.mark.asyncio
async def test_subagent_spill_reader_rereads_tail_before_synthesis() -> None:
    storage = _Storage()
    terminal_citation = "SEC-END-CITATION: Item-1A-page-147"
    evidence = "SEC filing\n" + ("material liquidity disclosure " * 5_000)
    evidence += terminal_citation
    registry = ToolRegistry()

    async def read_sec_filing(request: Any) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=evidence,
        )

    source_definition = ToolDefinition(
        name="read_sec_filing",
        description="Read complete SEC filing evidence",
        parameters=[],
        category=ToolCategory.RETRIEVAL,
        risk_level=ToolRiskLevel.LOW,
        capability_metadata={
            "operation_kind": "read",
            "output_sensitivity": "non_sensitive",
        },
    )
    registry.register(source_definition, read_sec_filing)
    registry.register(READ_TOOL_ARTIFACT_DEFINITION, ReadToolArtifactExecutor(storage))
    model = _ArtifactRereadModel()
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=registry,
        tool_invoker=RegistryToolInvoker(tool_registry=registry),
        artifact_storage=storage,
        tool_output_spill_enabled=True,
    )
    parent = _parent_context()

    events = [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.TASK,
                prompt="Read the filing, reread its artifact tail, then synthesize.",
            ),
            parent_user=parent.user,
            parent_tenant_id=parent.tenant_id,
            parent_invocation_context=parent,
        )
    ]

    finished = next(event for event in events if event["event_type"] == "subagent_finished")
    assert finished["data"]["status"] == "completed"
    assert "COMPLETE_REDACTED_ARTIFACT_RECEIPT" in model.receipt
    assert terminal_citation in model.reread
    assert hashlib.sha256(storage.created[0]["content"]).hexdigest() in model.reread
