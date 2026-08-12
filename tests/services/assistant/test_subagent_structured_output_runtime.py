from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest
from assistant_service.core.agent.agent_loop import AgentLoop
from assistant_service.core.agent.streaming_tool_call import _bound_parent_tool_content
from assistant_service.core.agent.subagent_manager import SubAgentManager
from assistant_service.core.agent.subagent_types import SubAgentConfig, SubAgentType
from assistant_service.core.run_budget import RunBudget, RunBudgetLimits
from assistant_service.core.tools.subagent_tool import (
    SPAWN_SUBAGENT_DEFINITION,
    SpawnSubAgentExecutor,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolRegistry,
    validate_tool_arguments,
)


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "risks": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["decision", "risks"],
        "additionalProperties": False,
    }


def _request(arguments: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        call_id="parent-spawn-call",
        tool_name="spawn_subagent",
        arguments=arguments,
    )


class _ScriptedModel:
    _models: dict[str, Any] = {}

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(self, **values: Any):
        self.calls.append(copy.deepcopy(values))
        response = self.responses[len(self.calls) - 1]
        yield SimpleNamespace(
            content=response.get("content", ""),
            tool_calls=response.get("tool_calls", []),
            finish_reason=response.get(
                "finish_reason",
                "tool_calls" if response.get("tool_calls") else "stop",
            ),
        )


def _budget(model_turns: int = 5) -> RunBudget:
    return RunBudget(
        RunBudgetLimits(
            max_model_turns=model_turns,
            max_tool_calls=5,
            max_parallel_tool_calls=2,
            max_wall_time_seconds=5,
            max_tool_result_bytes=10_000,
        )
    )


def test_parent_content_keeps_complete_child_contract_and_ordinary_evidence() -> None:
    complete_contract = "contract:" + "x" * 10_000

    assert (
        _bound_parent_tool_content(
            tool_name="spawn_subagent",
            content=complete_contract,
            tool_metadata={"subagent_result": {"schema_version": "assistant-subagent-result/v1"}},
        )
        == complete_contract
    )
    ordinary = _bound_parent_tool_content(
        tool_name="ordinary_action",
        content="x" * 10_000,
        tool_metadata={},
    )
    assert ordinary == "x" * 10_000


def test_parent_content_fails_closed_when_cap_has_no_complete_artifact() -> None:
    bounded = _bound_parent_tool_content(
        tool_name="read_statute",
        content="partial law text",
        tool_metadata={"response_cap_applied": True},
    )

    assert bounded.startswith("INCOMPLETE_TOOL_OUTPUT:")
    assert "partial law text" not in bounded
    assert "Do not treat omitted content as reviewed evidence" in bounded

    spoofed = _bound_parent_tool_content(
        tool_name="ordinary_action",
        content="partial action output",
        tool_metadata={
            "response_cap_applied": True,
            "subagent_result": {"schema_version": "assistant-subagent-result/v1"},
        },
    )
    assert spoofed.startswith("INCOMPLETE_TOOL_OUTPUT:")


def test_parent_content_accepts_verified_complete_artifact_preview() -> None:
    preview_and_receipt = "SEC preview\nCOMPLETE_REDACTED_ARTIFACT_RECEIPT: art-sec"

    bounded = _bound_parent_tool_content(
        tool_name="read_sec_filing",
        content=preview_and_receipt,
        tool_metadata={
            "response_cap_applied": True,
            "tool_output_artifact": {
                "artifact_id": "art-sec",
                "host_verified": True,
                "complete_redacted": True,
            },
        },
    )

    assert bounded == preview_and_receipt


async def _run(
    model: _ScriptedModel,
    *,
    max_turns: int = 4,
    run_budget: RunBudget | None = None,
) -> list[dict[str, Any]]:
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )
    return [
        event
        async for event in manager.spawn(
            SubAgentConfig(
                agent_type=SubAgentType.TASK,
                prompt="Review the bounded financial decision and return the contract.",
                description="Review financial decision",
                max_turns=max_turns,
                output_schema=_schema(),
            ),
            parent_attempt_id="attempt-structured-1",
            run_budget=run_budget,
        )
    ]


@pytest.mark.asyncio
async def test_spawn_marker_preserves_valid_output_schema_for_single_and_batch() -> None:
    executor = SpawnSubAgentExecutor()
    single_args = {
        "agent_type": "task",
        "prompt": "review",
        "description": "review",
        "output_schema": _schema(),
    }
    single = await executor.execute(_request(single_args))
    batch = await executor.execute(
        _request(
            {
                "tasks": [
                    single_args,
                    {
                        "agent_type": "plan",
                        "prompt": "plan",
                        "description": "plan",
                        "output_schema": _schema(),
                    },
                ],
                "max_concurrency": 2,
            }
        )
    )

    assert validate_tool_arguments(SPAWN_SUBAGENT_DEFINITION, single_args)["valid"] is True
    assert single.success is True
    assert SubAgentConfig.from_marker(single.result["config"]).output_schema == _schema()
    assert batch.success is True
    assert all(
        SubAgentConfig.from_marker(marker).output_schema == _schema()
        for marker in batch.result["configs"]
    )
    assert json.dumps(single.result)
    assert json.dumps(batch.result)


@pytest.mark.asyncio
async def test_spawn_executor_rejects_invalid_or_misplaced_output_schema() -> None:
    executor = SpawnSubAgentExecutor()
    open_schema = {
        "type": "object",
        "properties": {"decision": {"type": "string"}},
    }
    invalid = await executor.execute(
        _request(
            {
                "agent_type": "task",
                "prompt": "review",
                "description": "review",
                "output_schema": open_schema,
            }
        )
    )
    misplaced = await executor.execute(
        _request(
            {
                "tasks": [
                    {
                        "agent_type": "task",
                        "prompt": "review",
                        "description": "review",
                    }
                ],
                "output_schema": _schema(),
            }
        )
    )

    assert invalid.success is False
    assert "additionalProperties must be false" in str(invalid.error)
    assert misplaced.success is False
    assert "specified on each task" in str(misplaced.error)
    assert (
        validate_tool_arguments(
            SPAWN_SUBAGENT_DEFINITION,
            {
                "tasks": [
                    {
                        "agent_type": "task",
                        "prompt": "review",
                        "description": "review",
                    }
                ],
                "output_schema": _schema(),
            },
        )["valid"]
        is False
    )


@pytest.mark.asyncio
async def test_invalid_candidate_gets_exactly_one_budgeted_correction_then_completes() -> None:
    valid = '{"decision":"reject","risks":["unverified counterparty"]}'
    model = _ScriptedModel(
        [
            {"content": "not-json"},
            {"content": valid},
        ]
    )
    budget = _budget()

    events = await _run(model, run_budget=budget)

    deltas = [
        event["data"]["text"] for event in events if event["event_type"] == "subagent_text_delta"
    ]
    terminal = next(event["data"] for event in events if event["event_type"] == "subagent_finished")
    result = terminal["result"]
    assert deltas == [valid]
    assert terminal["status"] == "completed"
    assert result["schema_version"] == "assistant-subagent-result/v1"
    assert result["structured_payload"] == {
        "decision": "reject",
        "risks": ["unverified counterparty"],
    }
    assert result["usage"] == {
        "model_turns": 2,
        "tool_calls": 0,
        "output_characters": len(valid),
        "correction_rounds": 1,
        "duration_ms": terminal["duration_ms"],
    }
    assert budget.model_turns == 2
    assert len(model.calls) == 2
    assert model.calls[1]["tools"] is None
    correction_messages = model.calls[1]["messages"]
    assert correction_messages[-2] == {"role": "assistant", "content": "not-json"}
    assert correction_messages[-1]["role"] == "user"
    assert "failed the host-enforced" in correction_messages[-1]["content"]
    assert "not-json" not in correction_messages[-1]["content"]
    assert (
        AgentLoop._validate_subagent_terminal(
            terminal,
            expected_attempt_id="attempt-structured-1",
        )
        == result
    )
    formatted = AgentLoop._format_subagent_model_result(result)
    assert '"structured_payload"' in formatted
    assert '"correction_rounds": 1' in formatted


@pytest.mark.asyncio
async def test_second_invalid_candidate_fails_closed_without_leaking_raw_output() -> None:
    model = _ScriptedModel(
        [
            {"content": "first-invalid"},
            {"content": '{"decision":"approve","risks":[]}'},
        ]
    )

    events = await _run(model)

    assert [event for event in events if event["event_type"] == "subagent_text_delta"] == []
    terminal = next(event["data"] for event in events if event["event_type"] == "subagent_finished")
    assert terminal["status"] == "failed"
    assert terminal["result"]["structured_payload"] is None
    assert terminal["result"]["claims"] == []
    assert terminal["result"]["usage"]["correction_rounds"] == 1
    assert any(
        "Structured output" in limitation for limitation in terminal["result"]["limitations"]
    )
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_correction_round_cannot_escape_into_tool_execution() -> None:
    model = _ScriptedModel(
        [
            {"content": "invalid"},
            {
                "tool_calls": [
                    {
                        "id": "escape-call",
                        "type": "function",
                        "function": {"name": "unavailable_write", "arguments": "{}"},
                    }
                ]
            },
        ]
    )

    events = await _run(model)

    assert [event for event in events if event["event_type"] == "subagent_tool_start"] == []
    terminal = next(event["data"] for event in events if event["event_type"] == "subagent_finished")
    assert terminal["status"] == "failed"
    assert terminal["tool_calls"] == 0
    assert terminal["error"] == "Structured output correction attempted a tool call"
    assert len(model.calls) == 2
    assert model.calls[1]["tools"] is None


@pytest.mark.asyncio
async def test_no_correction_occurs_when_parent_turn_ceiling_has_no_capacity() -> None:
    model = _ScriptedModel([{"content": "invalid"}])

    events = await _run(model, max_turns=1)

    terminal = next(event["data"] for event in events if event["event_type"] == "subagent_finished")
    assert terminal["status"] == "failed"
    assert terminal["result"]["usage"]["model_turns"] == 1
    assert terminal["result"]["usage"]["correction_rounds"] == 0
    assert terminal["error"] == (
        "Structured output validation failed and no correction turn remained"
    )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_parent_budget_stops_correction_turn_without_sticky_exhaustion() -> None:
    model = _ScriptedModel(
        [
            {"content": "invalid"},
            {"content": '{"decision":"approve","risks":["never reached"]}'},
        ]
    )
    budget = _budget(model_turns=1)
    manager = SubAgentManager(
        model_registry=model,  # type: ignore[arg-type]
        tool_registry=ToolRegistry(),
    )

    events: list[dict[str, Any]] = []
    async for event in manager.spawn(
        SubAgentConfig(
            agent_type=SubAgentType.TASK,
            prompt="return structured review",
            max_turns=4,
            output_schema=_schema(),
        ),
        parent_attempt_id="attempt-budget",
        run_budget=budget,
    ):
        events.append(event)

    terminals = [event for event in events if event["event_type"] == "subagent_finished"]
    assert len(terminals) == 1
    assert terminals[0]["data"]["status"] == "failed"
    assert (
        terminals[0]["data"]["effective_execution"]["stop_reason"]
        == "parent_model_turn_budget_exhausted"
    )
    assert terminals[0]["data"]["result"]["usage"]["correction_rounds"] == 1
    assert len(model.calls) == 1
    assert budget.model_turns == 1
    assert budget.exhausted is False
