"""Public OpenAI Responses ingress contract tests.

These tests cover the transport projection only.  Provider-facing Responses
wire tests live in ``test_responses_api.py``; the ingress must always call the
existing AssistantService stream and therefore the one canonical AgentLoop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import pytest
from ai_gateway_core.comm import IdempotencyMiddleware, InMemoryIdempotencyStore
from assistant_service.api.routes import responses as responses_route
from assistant_service.api.routes.responses import (
    ResponsesIngressError,
    ResponsesStreamProjector,
    iter_responses_sse,
    parse_responses_request,
)
from assistant_service.api.routes.responses_projector import (
    ResponsesIngressError as ProjectorResponsesIngressError,
)
from assistant_service.api.routes.responses_projector import (
    ResponsesStreamProjector as CanonicalResponsesStreamProjector,
)
from assistant_service.core.assistant_service import AssistantStreamEvent
from assistant_service.core.models.model_registry import ModelProvider
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _sse_payload(line: str) -> dict[str, Any]:
    data_line = next(part for part in line.splitlines() if part.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def _assert_partial_failure_lifecycle(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    assert [event["type"] for event in events] == [
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.failed",
    ]
    failed = events[-1]
    assert failed["response"]["status"] == "failed"
    assert failed["response"]["output"][0]["status"] == "incomplete"
    return failed


def test_responses_route_reexports_projector_contract_identity() -> None:
    assert ResponsesIngressError is ProjectorResponsesIngressError
    assert ResponsesStreamProjector is CanonicalResponsesStreamProjector
    assert responses_route.ResponsesIngressError is ProjectorResponsesIngressError
    assert responses_route.ResponsesStreamProjector is CanonicalResponsesStreamProjector
    assert ResponsesIngressError.__module__ == responses_route.__name__
    assert ResponsesStreamProjector.__module__ == responses_route.__name__
    assert {"ResponsesIngressError", "ResponsesStreamProjector"} <= set(responses_route.__all__)


@pytest.mark.asyncio
async def test_responses_route_projector_reexport_remains_stream_monkeypatch_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, Any]] = []

    class TrackingProjector(CanonicalResponsesStreamProjector):
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)
            super().__init__(**kwargs)

    class EmptyAssistant:
        async def chat_stream(self, **_kwargs: Any):
            if False:
                yield

        def clear_session_runtime_state(self, **_kwargs: Any) -> dict[str, Any]:
            return {"cleared": True}

    monkeypatch.setattr(responses_route, "ResponsesStreamProjector", TrackingProjector)
    parsed = parse_responses_request(
        {"model": "qwen3.7-plus", "input": "hello", "stream": True}
    )
    frames = [
        _sse_payload(frame)
        async for frame in responses_route.iter_responses_sse(
            assistant=EmptyAssistant(),
            parsed=parsed,
            user=_User(),
            response_id="resp_test",
            session_id="session-test",
        )
    ]

    assert len(constructed) == 1
    assert constructed[0]["response_id"] == "resp_test"
    assert [frame["type"] for frame in frames] == ["response.created", "response.failed"]


def test_parse_text_request_is_ephemeral_and_disables_all_platform_tools() -> None:
    parsed = parse_responses_request(
        {
            "model": "qwen3.7-plus",
            "input": "Explain the release gate",
            "instructions": "Be concise",
            "temperature": 0.2,
            "max_output_tokens": 512,
            "stream": True,
        }
    )

    assert parsed.message == "Explain the release gate"
    assert parsed.history == []
    assert parsed.model_id == "qwen3.7-plus"
    assert parsed.instructions == "Be concise"
    assert parsed.temperature == 0.2
    assert parsed.max_output_tokens == 512
    assert parsed.stream is True
    assert parsed.store is False
    assert parsed.config.capability_allowlist is not None
    assert parsed.config.capability_allowlist.tool_names == frozenset()


def test_parse_stateless_function_output_continuation_preserves_exact_pairing() -> None:
    parsed = parse_responses_request(
        {
            "model": "qwen3.7-plus",
            "input": [
                {"role": "user", "content": "Look up order 42"},
                {
                    "type": "function_call",
                    "call_id": "call_42",
                    "name": "lookup_order",
                    "arguments": '{"order_id":42}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_42",
                    "output": '{"status":"shipped"}',
                },
            ],
        }
    )

    assert parsed.message == "Continue using the supplied function result."
    assert parsed.history == [
        {"role": "user", "content": "Look up order 42"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_42",
                    "type": "function",
                    "function": {
                        "name": "lookup_order",
                        "arguments": '{"order_id":42}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": '{"status":"shipped"}',
            "tool_call_id": "call_42",
            "name": "lookup_order",
        },
    ]


@pytest.mark.parametrize(
    ("patch", "code", "param"),
    [
        (
            {"previous_response_id": "resp_old"},
            "previous_response_id_not_supported",
            "previous_response_id",
        ),
        ({"store": True}, "store_not_supported", "store"),
        (
            {"tools": [{"type": "web_search"}]},
            "built_in_tools_not_supported",
            "tools",
        ),
        (
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {"type": "object"},
                    }
                ]
            },
            "client_function_tools_not_supported",
            "tools",
        ),
        ({"background": True}, "unsupported_field", "background"),
        ({"instructions": "x" * 501}, "invalid_string", "instructions"),
    ],
)
def test_parse_rejects_unsupported_stateful_or_tool_contracts(
    patch: dict[str, Any], code: str, param: str
) -> None:
    payload: dict[str, Any] = {"model": "qwen3.7-plus", "input": "hello", **patch}

    with pytest.raises(ResponsesIngressError) as exc_info:
        parse_responses_request(payload)

    assert exc_info.value.code == code
    assert exc_info.value.param == param


@pytest.mark.parametrize(
    "input_items",
    [
        [{"type": "function_call_output", "call_id": "missing", "output": "x"}],
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_2", "output": "x"},
        ],
        [
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "not-json",
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "x"},
        ],
    ],
)
def test_parse_rejects_orphan_mismatched_or_invalid_function_continuations(
    input_items: list[dict[str, Any]],
) -> None:
    with pytest.raises(ResponsesIngressError) as exc_info:
        parse_responses_request({"model": "qwen3.7-plus", "input": input_items})

    assert exc_info.value.code in {
        "orphan_function_call_output",
        "invalid_function_arguments",
    }


def test_stream_projector_emits_monotonic_text_lifecycle_and_usage() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    events: list[dict[str, Any]] = []
    events.extend(projector.created())
    events.extend(projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"})))
    events.extend(projector.accept(AssistantStreamEvent("text_delta", "Hello")))
    events.extend(
        projector.accept(
            AssistantStreamEvent(
                "usage",
                {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 3},
            )
        )
    )
    events.extend(
        projector.accept(
            AssistantStreamEvent(
                "done",
                {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "total_length": 5,
                },
            )
        )
    )
    assert events[-1]["type"] == "response.output_text.delta"
    events.extend(
        projector.accept(
            AssistantStreamEvent(
                "run_finished",
                {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "terminal_envelope": {
                        "run_id": "run-1",
                        "session_id": "session-test",
                        "model_id": "qwen3.7-plus",
                        "status": "succeeded",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "cached_input_tokens": 3,
                        },
                    },
                },
            )
        )
    )

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    completed = events[-1]["response"]
    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "Hello"
    assert completed["usage"] == {
        "input_tokens": 10,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 12,
    }
    assert completed["metadata"] == {
        "ai_gateway_run_id": "run-1",
        "ai_gateway_session_id": "session-test",
    }


@pytest.mark.parametrize(
    "event_type",
    [
        "tool_call",
        "tool_result",
        "tool_call_start",
        "tool_call_result",
        "tool_call_end",
        "tool_call_cancelled",
    ],
)
def test_stream_projector_fails_closed_on_any_tool_event(event_type: str) -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    projector.created()

    events = projector.accept(
        AssistantStreamEvent(
            event_type,
            {
                "tool_call_id": "call-1",
                "name": "lookup",
                "arguments": {"query": "x"},
                "run_id": "run-1",
            },
        )
    )

    assert [event["type"] for event in events] == ["response.failed"]
    assert events[0]["response"]["error"]["code"] == "unexpected_tool_event"


@pytest.mark.parametrize(
    "event",
    [
        AssistantStreamEvent("text_delta", "late"),
        AssistantStreamEvent("usage", {"input_tokens": 1, "output_tokens": 1}),
        AssistantStreamEvent(
            "tool_call_start",
            {"run_id": "run-1", "tool_call_id": "call-1", "name": "lookup"},
        ),
        AssistantStreamEvent("run_started", {"run_id": "run-1"}),
    ],
)
def test_stream_projector_accepts_only_authoritative_terminal_after_transport_done(
    event: AssistantStreamEvent,
) -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("text_delta", "Hello"))
    projector.accept(AssistantStreamEvent("done", {"run_id": "run-1", "total_length": 5}))

    events = projector.accept(event)

    failed = _assert_partial_failure_lifecycle(events)
    assert failed["response"]["error"]["code"] == "event_after_transport_done"


def test_stream_projector_requires_run_id_on_first_run_lifecycle() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    projector.created()

    events = projector.accept(AssistantStreamEvent("run_started", {}))

    assert [event["type"] for event in events] == ["response.failed"]
    assert events[0]["response"]["error"]["code"] == "missing_run_identity"


def test_stream_projector_emits_one_failed_terminal_for_run_error() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    events = projector.created()
    events.extend(
        projector.accept(
            AssistantStreamEvent(
                "run_error",
                {
                    "run_id": "run-1",
                    "error": "provider request failed",
                    "terminal_envelope": {
                        "run_id": "run-1",
                        "session_id": "session-test",
                        "model_id": "qwen3.7-plus",
                        "status": "failed",
                        "exit_reason": "server_error",
                        "usage": {},
                    },
                },
            )
        )
    )

    assert [event["type"] for event in events] == [
        "response.created",
        "response.failed",
    ]
    failed = events[-1]["response"]
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "server_error"
    assert "provider request failed" not in failed["error"]["message"]


def test_failed_response_closes_partial_message_and_preserves_text() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("text_delta", "partial"))

    events = projector.fail(code="server_error")

    failed = _assert_partial_failure_lifecycle(events)["response"]
    assert failed["status"] == "failed"
    assert failed["output"] == [
        {
            "id": projector.output[0]["id"],
            "type": "message",
            "status": "incomplete",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "partial", "annotations": []}
            ],
        }
    ]
    from openai.types.responses import Response

    parsed = Response.model_validate(failed)
    assert parsed.output[0].status == "incomplete"


def test_stream_projector_binds_cancelled_run_error_to_authoritative_usage() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("usage", {"input_tokens": 999, "output_tokens": 999}))

    events = projector.accept(
        AssistantStreamEvent(
            "run_error",
            {
                "run_id": "run-1",
                "terminal_envelope": {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                    "model_id": "qwen3.7-plus",
                    "status": "cancelled",
                    "exit_reason": "cancelled",
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            },
        )
    )

    assert [event["type"] for event in events] == ["response.failed"]
    failed = events[0]["response"]
    assert failed["error"]["code"] == "cancelled"
    assert failed["usage"]["input_tokens"] == 7
    assert failed["usage"]["output_tokens"] == 2
    assert failed["usage"]["total_tokens"] == 9


def test_stream_projector_rejects_run_error_without_authoritative_usage() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
    )
    projector.created()

    events = projector.accept(
        AssistantStreamEvent(
            "run_error",
            {
                "run_id": "run-1",
                "terminal_envelope": {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "model_id": "qwen3.7-plus",
                    "status": "failed",
                    "exit_reason": "model_error",
                },
            },
        )
    )

    assert [event["type"] for event in events] == ["response.failed"]
    assert events[0]["response"]["error"]["code"] == "missing_terminal_usage"


def test_stream_projector_fails_closed_on_terminal_identity_or_usage_mismatch() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("text_delta", "Hello"))
    projector.accept(AssistantStreamEvent("usage", {"input_tokens": 5, "output_tokens": 1}))
    projector.accept(AssistantStreamEvent("done", {"run_id": "run-1", "total_length": 5}))

    events = projector.accept(
        AssistantStreamEvent(
            "run_finished",
            {
                "run_id": "run-1",
                "terminal_envelope": {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "tenant_id": "tenant-other",
                    "user_id": "user-1",
                    "model_id": "qwen3.7-plus",
                    "status": "succeeded",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            },
        )
    )

    failed = _assert_partial_failure_lifecycle(events)
    assert failed["response"]["error"]["code"] == "terminal_identity_mismatch"


def test_stream_projector_fails_closed_on_terminal_usage_mismatch() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("text_delta", "Hello"))
    projector.accept(AssistantStreamEvent("usage", {"input_tokens": 5, "output_tokens": 1}))
    projector.accept(AssistantStreamEvent("done", {"run_id": "run-1", "total_length": 5}))

    events = projector.accept(
        AssistantStreamEvent(
            "run_finished",
            {
                "run_id": "run-1",
                "terminal_envelope": {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                    "model_id": "qwen3.7-plus",
                    "status": "succeeded",
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            },
        )
    )

    failed = _assert_partial_failure_lifecycle(events)
    assert failed["response"]["error"]["code"] == "terminal_usage_mismatch"


def test_stream_projector_requires_transport_done_before_success() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    projector.created()
    projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"}))
    projector.accept(AssistantStreamEvent("text_delta", "Hello"))
    projector.accept(AssistantStreamEvent("usage", {"input_tokens": 5, "output_tokens": 1}))

    events = projector.accept(
        AssistantStreamEvent(
            "run_finished",
            {
                "run_id": "run-1",
                "terminal_envelope": {
                    "run_id": "run-1",
                    "session_id": "session-test",
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                    "model_id": "qwen3.7-plus",
                    "status": "succeeded",
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            },
        )
    )

    failed = _assert_partial_failure_lifecycle(events)
    assert failed["response"]["error"]["code"] == "missing_transport_done"


def test_stream_projector_requires_a_bound_run_identity() -> None:
    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions=None,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    projector.created()
    projector.accept(AssistantStreamEvent("text_delta", "Hello"))
    projector.accept(AssistantStreamEvent("usage", {"input_tokens": 5, "output_tokens": 1}))
    events = projector.accept(AssistantStreamEvent("done", {"total_length": 5}))

    failed = _assert_partial_failure_lifecycle(events)
    assert failed["response"]["error"]["code"] == "missing_run_identity"


def test_completed_response_validates_with_installed_openai_sdk_model() -> None:
    from openai.types.responses import Response

    projector = ResponsesStreamProjector(
        response_id="resp_test",
        session_id="session-test",
        model="qwen3.7-plus",
        instructions="Be concise",
        temperature=0.2,
        tenant_id="tenant-1",
        user_id="user-1",
    )
    events = projector.created()
    events.extend(projector.accept(AssistantStreamEvent("run_started", {"run_id": "run-1"})))
    events.extend(projector.accept(AssistantStreamEvent("text_delta", "Hello")))
    events.extend(
        projector.accept(AssistantStreamEvent("usage", {"input_tokens": 5, "output_tokens": 1}))
    )
    events.extend(
        projector.accept(AssistantStreamEvent("done", {"run_id": "run-1", "total_length": 5}))
    )
    events.extend(
        projector.accept(
            AssistantStreamEvent(
                "run_finished",
                {
                    "run_id": "run-1",
                    "terminal_envelope": {
                        "run_id": "run-1",
                        "session_id": "session-test",
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                        "model_id": "qwen3.7-plus",
                        "status": "succeeded",
                        "usage": {"input_tokens": 5, "output_tokens": 1},
                    },
                },
            )
        )
    )

    parsed = Response.model_validate(events[-1]["response"])
    assert parsed.id == "resp_test"
    assert parsed.status == "completed"


@pytest.mark.asyncio
async def test_sse_generator_propagates_client_close_to_canonical_stream() -> None:
    closed = asyncio.Event()

    class FakeAssistant:
        async def chat_stream(self, **_kwargs: Any):
            try:
                yield AssistantStreamEvent("run_started", {"run_id": "run-1"})
                await asyncio.Event().wait()
            finally:
                closed.set()

        def clear_session_runtime_state(self, **_kwargs: Any) -> dict[str, Any]:
            return {"cleared": True}

    parsed = parse_responses_request({"model": "qwen3.7-plus", "input": "hello", "stream": True})
    user = _User()
    stream = iter_responses_sse(
        assistant=FakeAssistant(),
        parsed=parsed,
        user=user,
        response_id="resp_test",
        session_id="session-test",
    )

    first = _sse_payload(await anext(stream))
    assert first["type"] == "response.created"
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_after_first", "expected_log_event"),
    [
        (False, "assistant.responses.stream.startup_failed"),
        (True, "assistant.responses.stream.failed"),
    ],
)
async def test_responses_stream_internal_failure_is_diagnostic_but_publicly_generic(
    fail_after_first: bool,
    expected_log_event: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_exception_message = "private-responses-exception-message"

    class FailingAssistant:
        async def chat_stream(self, **_kwargs: Any):
            if fail_after_first:
                yield AssistantStreamEvent("run_started", {"run_id": "run-1"})
                yield AssistantStreamEvent("text_delta", "partial")
            raise RuntimeError(raw_exception_message)

        def clear_session_runtime_state(self, **_kwargs: Any) -> dict[str, Any]:
            return {"cleared": True}

    parsed = parse_responses_request(
        {"model": "qwen3.7-plus", "input": "hello", "stream": True}
    )
    with caplog.at_level(
        logging.ERROR,
        logger="assistant_service.api.routes.responses",
    ):
        frames = [
            _sse_payload(frame)
            async for frame in iter_responses_sse(
                assistant=FailingAssistant(),
                parsed=parsed,
                user=_User(),
                response_id="resp_test",
                session_id="session-test",
            )
        ]

    assert frames[-1]["type"] == "response.failed"
    assert frames[-1]["response"]["error"]["message"] == (
        "The response could not be completed."
    )
    if fail_after_first:
        assert frames[-1]["response"]["output"][0]["status"] == "incomplete"
        assert frames[-1]["response"]["output"][0]["content"][0]["text"] == "partial"
    assert raw_exception_message not in json.dumps(frames)
    records = [record for record in caplog.records if record.getMessage().startswith(expected_log_event)]
    assert len(records) == 1
    record = records[0]
    assert record.exc_info is None
    diagnostic = record.internal_exception
    assert diagnostic["exception_type"] == "RuntimeError"
    assert diagnostic["frames"]
    assert raw_exception_message not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_type", "terminal_status", "expected_response_type"),
    [
        ("run_finished", "succeeded", "response.completed"),
        ("run_error", "failed", "response.failed"),
    ],
)
async def test_sse_generator_projects_authoritative_terminal_without_waiting_for_source_exhaustion(
    terminal_type: str,
    terminal_status: str,
    expected_response_type: str,
) -> None:
    closed = asyncio.Event()

    class TerminalThenBlockedAssistant:
        async def chat_stream(self, **kwargs: Any):
            try:
                yield AssistantStreamEvent("run_started", {"run_id": "run-1"})
                yield AssistantStreamEvent("text_delta", "partial")
                yield AssistantStreamEvent("usage", {"input_tokens": 4, "output_tokens": 1})
                if terminal_type == "run_finished":
                    yield AssistantStreamEvent(
                        "done", {"run_id": "run-1", "total_length": len("partial")}
                    )
                terminal_envelope = {
                    "run_id": "run-1",
                    "session_id": kwargs["session_id"],
                    "tenant_id": kwargs["user"].tenant_id,
                    "user_id": kwargs["user"].user_id,
                    "model_id": kwargs["config"].model_id,
                    "status": terminal_status,
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                }
                if terminal_type == "run_error":
                    terminal_envelope["exit_reason"] = "server_error"
                yield AssistantStreamEvent(
                    terminal_type,
                    {
                        "run_id": "run-1",
                        "terminal_envelope": terminal_envelope,
                    },
                )
                await asyncio.Event().wait()
            finally:
                closed.set()

        def clear_session_runtime_state(self, **_kwargs: Any) -> dict[str, Any]:
            return {"cleared": True}

    parsed = parse_responses_request({"model": "qwen3.7-plus", "input": "hello", "stream": True})
    stream = iter_responses_sse(
        assistant=TerminalThenBlockedAssistant(),
        parsed=parsed,
        user=_User(),
        response_id="resp_test",
        session_id="session-test",
    )

    async def collect() -> list[dict[str, Any]]:
        return [_sse_payload(frame) async for frame in stream]

    frames = await asyncio.wait_for(collect(), timeout=0.5)

    assert frames[-1]["type"] == expected_response_type
    assert [frame["sequence_number"] for frame in frames] == list(range(len(frames)))
    await asyncio.wait_for(closed.wait(), timeout=0.1)


class _ModelRegistry:
    def get_model(self, model_id: str):
        if model_id != "qwen3.7-plus":
            return None
        return type(
            "Model",
            (),
            {"provider": ModelProvider.DASHSCOPE, "max_output_tokens": 8192},
        )()

    def is_provider_configured(self, _provider: ModelProvider) -> bool:
        return True


class _SuccessfulAssistant:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.cleared: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any):
        self.calls.append(kwargs)
        yield AssistantStreamEvent("run_started", {"run_id": f"run-{len(self.calls)}"})
        yield AssistantStreamEvent("text_delta", "Hello")
        yield AssistantStreamEvent(
            "usage", {"input_tokens": 4, "output_tokens": 1, "cached_input_tokens": 0}
        )
        yield AssistantStreamEvent(
            "done",
            {
                "run_id": f"run-{len(self.calls)}",
                "total_length": 5,
            },
        )
        yield AssistantStreamEvent(
            "run_finished",
            {
                "run_id": f"run-{len(self.calls)}",
                "session_id": kwargs["session_id"],
                "terminal_envelope": {
                    "run_id": f"run-{len(self.calls)}",
                    "session_id": kwargs["session_id"],
                    "tenant_id": kwargs["user"].tenant_id,
                    "user_id": kwargs["user"].user_id,
                    "model_id": kwargs["config"].model_id,
                    "status": "succeeded",
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 1,
                        "cached_input_tokens": 0,
                    },
                },
            },
        )

    def clear_session_runtime_state(self, **kwargs: Any) -> dict[str, Any]:
        self.cleared.append(kwargs)
        return {"cleared": True}


def _route_app(
    assistant: Any,
    *,
    with_idempotency: bool = False,
    trusted_gateway: bool = True,
) -> FastAPI:
    from assistant_service.api.routes import responses as responses_route

    app = FastAPI()
    app.include_router(responses_route.router)
    app.state.assistant_service = assistant
    app.state.model_registry = _ModelRegistry()
    app.dependency_overrides[responses_route.get_user_context] = lambda: _User()
    if trusted_gateway:

        @app.middleware("http")
        async def mark_gateway_verified(request: Request, call_next):
            request.state.gateway_secret_verified = True
            return await call_next(request)

    if with_idempotency:
        app.add_middleware(
            IdempotencyMiddleware,
            store=InMemoryIdempotencyStore(),
        )
    return app


def test_nonstream_route_returns_response_object_from_canonical_stream() -> None:
    assistant = _SuccessfulAssistant()

    with TestClient(_route_app(assistant)) as client:
        response = client.post(
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello", "temperature": 0.1},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["temperature"] == 0.1
    assert payload["store"] is False
    assert payload["output"][0]["content"][0]["text"] == "Hello"
    assert payload["metadata"]["ai_gateway_run_id"] == "run-1"
    call = assistant.calls[0]
    assert call["persist_messages"] is False
    assert call["config"].capability_allowlist.tool_names == frozenset()
    assert call["config"].model_provider is ModelProvider.DASHSCOPE
    assert assistant.cleared[0]["tenant_id"] == "tenant-1"


def test_internal_route_rejects_forged_identity_without_verified_gateway() -> None:
    assistant = _SuccessfulAssistant()

    with TestClient(_route_app(assistant, trusted_gateway=False)) as client:
        response = client.post(
            "/responses",
            headers={"X-User-Id": "forged", "X-Tenant-Id": "tenant-victim"},
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "gateway_authentication_required"
    assert assistant.calls == []


def test_internal_route_accepts_verified_authenticated_default_tenant() -> None:
    from assistant_service.api.routes import responses as responses_route

    assistant = _SuccessfulAssistant()
    app = _route_app(assistant)
    app.dependency_overrides[responses_route.get_user_context] = lambda: _User(tenant_id="default")

    with TestClient(app) as client:
        response = client.post(
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 200
    assert assistant.calls[0]["user"].tenant_id == "default"


def test_internal_route_rejects_verified_public_tenant() -> None:
    from assistant_service.api.routes import responses as responses_route

    assistant = _SuccessfulAssistant()
    app = _route_app(assistant)
    app.dependency_overrides[responses_route.get_user_context] = lambda: _User(tenant_id="public")

    with TestClient(app) as client:
        response = client.post(
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert assistant.calls == []


@pytest.mark.parametrize("registry", [None, object()])
def test_internal_route_fails_closed_when_model_registry_is_unavailable(registry: Any) -> None:
    assistant = _SuccessfulAssistant()
    app = _route_app(assistant)
    app.state.model_registry = registry

    with TestClient(app) as client:
        response = client.post(
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_registry_unavailable"
    assert assistant.calls == []


@pytest.mark.parametrize(
    "registry",
    [
        None,
        _ModelRegistry(),
        type(
            "UnconfiguredTenantModelRegistry",
            (),
            {
                "get_model": lambda _self, _model_id: type(
                    "Model",
                    (),
                    {"provider": ModelProvider.OPENAI, "max_output_tokens": 4096},
                )(),
                "is_provider_configured": lambda _self, _provider: False,
            },
        )(),
    ],
    ids=["missing-registry", "model-missing", "provider-unconfigured"],
)
def test_tenant_resolver_defers_global_preflight_for_exact_tenant_only_model(
    registry: Any,
) -> None:
    assistant = _SuccessfulAssistant()
    assistant.tenant_model_registry_resolver = object()
    app = _route_app(assistant)
    app.state.model_registry = registry

    with TestClient(app) as client:
        response = client.post(
            "/responses",
            json={"model": "tenant-only-model", "input": "hello"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["model"] == "tenant-only-model"
    assert assistant.calls[0]["config"].model_id == "tenant-only-model"


def test_stream_route_uses_strict_sse_event_names_and_sequence() -> None:
    assistant = _SuccessfulAssistant()

    with (
        TestClient(_route_app(assistant)) as client,
        client.stream(
            "POST",
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello", "stream": True},
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert response.status_code == 200
    frames = [frame for frame in body.split("\n\n") if frame.strip()]
    payloads = [_sse_payload(frame) for frame in frames]
    assert [payload["type"] for payload in payloads] == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [payload["sequence_number"] for payload in payloads] == list(range(len(payloads)))


def test_stream_route_emits_canonical_heartbeat_while_first_event_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowFirstEventAssistant(_SuccessfulAssistant):
        async def chat_stream(self, **kwargs: Any):
            await asyncio.sleep(0.04)
            async for event in super().chat_stream(**kwargs):
                yield event

    monkeypatch.setattr(
        responses_route,
        "_SSE_HEARTBEAT_INTERVAL_S",
        0.01,
        raising=False,
    )

    with (
        TestClient(_route_app(SlowFirstEventAssistant())) as client,
        client.stream(
            "POST",
            "/responses",
            json={"model": "qwen3.7-plus", "input": "hello", "stream": True},
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert body.count(": heartbeat\n\n") >= 2
    assert "event: response.completed" in body
    event_frames = [frame for frame in body.split("\n\n") if frame.startswith("event: ")]
    payloads = [_sse_payload(frame) for frame in event_frames]
    assert [payload["sequence_number"] for payload in payloads] == list(range(len(payloads)))


def test_nonstream_idempotency_is_scoped_by_gateway_tenant_and_user_headers() -> None:
    from assistant_service.api.routes import responses as responses_route

    assistant = _SuccessfulAssistant()
    app = _route_app(assistant, with_idempotency=True)

    async def header_user(request: Request) -> _User:
        return _User(
            user_id=request.headers["x-user-id"],
            tenant_id=request.headers["x-tenant-id"],
        )

    app.dependency_overrides[responses_route.get_user_context] = header_user
    body = {"model": "qwen3.7-plus", "input": "hello"}
    common = {"Idempotency-Key": "idem-1", "X-User-Id": "user-1"}

    with TestClient(app) as client:
        first = client.post("/responses", json=body, headers={**common, "X-Tenant-Id": "tenant-a"})
        replay = client.post("/responses", json=body, headers={**common, "X-Tenant-Id": "tenant-a"})
        other_tenant = client.post(
            "/responses", json=body, headers={**common, "X-Tenant-Id": "tenant-b"}
        )

    assert first.status_code == replay.status_code == other_tenant.status_code == 200
    assert replay.headers["x-idempotency-replayed"] == "true"
    assert first.json()["id"] == replay.json()["id"]
    assert other_tenant.json()["id"] != first.json()["id"]
    assert len(assistant.calls) == 2


def test_nonstream_failure_remains_a_response_object_with_bound_run() -> None:
    class FailedAssistant(_SuccessfulAssistant):
        async def chat_stream(self, **kwargs: Any):
            self.calls.append(kwargs)
            yield AssistantStreamEvent("run_started", {"run_id": "run-failed"})
            yield AssistantStreamEvent(
                "approval_required",
                {
                    "run_id": "run-failed",
                    "approval_id": "approval-1",
                    "session_id": kwargs["session_id"],
                },
            )

    with TestClient(_route_app(FailedAssistant())) as client:
        response = client.post(
            "/responses", json={"model": "qwen3.7-plus", "input": "write the file"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "approval_required"
    assert payload["metadata"]["ai_gateway_run_id"] == "run-failed"


def test_route_rejects_output_budget_above_exact_model_limit() -> None:
    with TestClient(_route_app(_SuccessfulAssistant())) as client:
        response = client.post(
            "/responses",
            json={
                "model": "qwen3.7-plus",
                "input": "hello",
                "max_output_tokens": 8193,
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "max_output_tokens_exceeds_model_limit"


def test_assistant_application_registers_one_internal_responses_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-secret")
    from assistant_service.main import app

    operation = app.openapi()["paths"]["/api/v1/assistant/responses"]
    assert set(operation) == {"post"}


@dataclass
class _User:
    user_id: str = "user-1"
    tenant_id: str = "tenant-1"
    tier: str = "normal"
    roles: list[str] | None = None
