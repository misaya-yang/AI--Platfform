"""OpenAI Responses event projection over canonical Assistant stream events."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Literal

from ...core.assistant_service import AssistantStreamEvent


class ResponsesIngressError(ValueError):
    """Safe client-facing Responses request or projection failure."""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        param: str | None = None,
        status_code: int = 400,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code.replace("_", " ")
        self.param = param
        self.status_code = status_code
        self.error_type = error_type


def _usage(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        }
    if not isinstance(value, dict):
        raise ResponsesIngressError("invalid_usage", status_code=500)

    def token(name: str, *aliases: str) -> int:
        raw: Any = None
        for key in (name, *aliases):
            if key in value:
                raw = value[key]
                break
        if raw is None:
            return 0
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ResponsesIngressError("invalid_usage", status_code=500)
        return raw

    input_tokens = token("input_tokens", "prompt_tokens")
    output_tokens = token("output_tokens", "completion_tokens")
    details = value.get("input_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise ResponsesIngressError("invalid_usage", status_code=500)
    cached_tokens = token("cached_input_tokens")
    if cached_tokens == 0 and isinstance(details, dict) and "cached_tokens" in details:
        cached_raw = details["cached_tokens"]
        if isinstance(cached_raw, bool) or not isinstance(cached_raw, int) or cached_raw < 0:
            raise ResponsesIngressError("invalid_usage", status_code=500)
        cached_tokens = cached_raw
    if cached_tokens > input_tokens:
        raise ResponsesIngressError("invalid_usage", status_code=500)
    total_tokens = input_tokens + output_tokens
    supplied_total = value.get("total_tokens")
    if supplied_total is not None and (
        isinstance(supplied_total, bool)
        or not isinstance(supplied_total, int)
        or supplied_total != total_tokens
    ):
        raise ResponsesIngressError("invalid_usage", status_code=500)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached_tokens},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": token("reasoning_tokens")},
        "total_tokens": total_tokens,
    }


class ResponsesStreamProjector:
    """Stateful, strictly sequenced projection of canonical Assistant events."""

    def __init__(
        self,
        *,
        response_id: str,
        session_id: str,
        model: str,
        instructions: str | None,
        temperature: float | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self.response_id = response_id
        self.session_id = session_id
        self.model = model
        self.instructions = instructions
        self.temperature = temperature
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.created_at = int(time.time())
        self.sequence_number = 0
        self.run_id: str | None = None
        self.terminal = False
        self.output: list[dict[str, Any]] = []
        self.usage = _usage({})
        self._usage_seen = False
        self._transport_done: dict[str, Any] | None = None
        self._message_item: dict[str, Any] | None = None
        self._message_output_index: int | None = None
        self._text = ""
        self._run_started_seen = False

    def _event(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event = {
            "type": event_type,
            "sequence_number": self.sequence_number,
            **payload,
        }
        self.sequence_number += 1
        return event

    def _response(self, *, status: str, error: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = {"ai_gateway_session_id": self.session_id}
        if self.run_id:
            metadata["ai_gateway_run_id"] = self.run_id
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "error": error,
            "incomplete_details": None,
            "instructions": self.instructions,
            "model": self.model,
            "output": copy.deepcopy(self.output),
            "parallel_tool_calls": False,
            "previous_response_id": None,
            "store": False,
            "temperature": self.temperature,
            "tool_choice": "none",
            "tools": [],
            "usage": copy.deepcopy(self.usage) if status != "in_progress" else None,
            "metadata": metadata,
        }

    def created(self) -> list[dict[str, Any]]:
        return [self._event("response.created", response=self._response(status="in_progress"))]

    @staticmethod
    def _valid_run_id(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and value.strip() == value
            and value
            and len(value) <= 255
            and "\x00" not in value
        )

    def _bind_run(self, value: Any, *, required: bool = False) -> list[dict[str, Any]]:
        if value is None or value == "":
            return self.fail(code="missing_run_identity") if required else []
        if not self._valid_run_id(value):
            return self.fail(code="invalid_run_identity")
        candidate = value
        if self.run_id is not None and candidate != self.run_id:
            return self.fail(code="run_identity_mismatch")
        self.run_id = candidate
        return []

    @staticmethod
    def _event_type(event: AssistantStreamEvent) -> str:
        raw = event.event_type
        return str(getattr(raw, "value", raw) or "")

    @staticmethod
    def _data(event: AssistantStreamEvent) -> dict[str, Any]:
        return dict(event.data) if isinstance(event.data, dict) else {}

    def _start_message(self) -> list[dict[str, Any]]:
        if self._message_item is not None:
            return []
        output_index = len(self.output)
        item = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        self.output.append(item)
        self._message_item = item
        self._message_output_index = output_index
        part = {"type": "output_text", "text": "", "annotations": []}
        return [
            self._event(
                "response.output_item.added",
                output_index=output_index,
                item=copy.deepcopy(item),
            ),
            self._event(
                "response.content_part.added",
                item_id=item["id"],
                output_index=output_index,
                content_index=0,
                part=part,
            ),
        ]

    def _text_delta(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = value.get("delta", value.get("content", value.get("text")))
        if not isinstance(value, str) or not value:
            return self.fail(code="invalid_text_delta")
        events = self._start_message()
        if self.terminal:
            return events
        self._text += value
        assert self._message_item is not None
        assert self._message_output_index is not None
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                delta=value,
                logprobs=[],
            )
        )
        return events

    def _close_message(
        self,
        *,
        status: Literal["completed", "incomplete"] = "completed",
    ) -> list[dict[str, Any]]:
        if self._message_item is None or self._message_item["status"] in {
            "completed",
            "incomplete",
        }:
            return []
        assert self._message_output_index is not None
        part = {"type": "output_text", "text": self._text, "annotations": []}
        self._message_item["status"] = status
        self._message_item["content"] = [part]
        return [
            self._event(
                "response.output_text.done",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                text=self._text,
                logprobs=[],
            ),
            self._event(
                "response.content_part.done",
                item_id=self._message_item["id"],
                output_index=self._message_output_index,
                content_index=0,
                part=copy.deepcopy(part),
            ),
            self._event(
                "response.output_item.done",
                output_index=self._message_output_index,
                item=copy.deepcopy(self._message_item),
            ),
        ]

    def accept(self, event: AssistantStreamEvent) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("event_after_terminal", status_code=500)
        event_type = self._event_type(event)
        data = self._data(event)
        is_tool_event = event_type.startswith("tool_")
        authoritative_terminals = {"run_finished", "run_error"}
        if self._transport_done is not None and event_type not in authoritative_terminals:
            return self.fail(code="event_after_transport_done")
        run_id_required = (
            event_type
            in {
                "run_started",
                "done",
                "run_finished",
                "run_error",
                "approval_required",
                "side_effect_unknown",
                "tool_call_start",
                "tool_call_end",
            }
            or is_tool_event
        )
        bound = self._bind_run(data.get("run_id"), required=run_id_required)
        if bound or self.terminal:
            return bound

        if event_type == "run_started":
            if self._run_started_seen:
                return self.fail(code="duplicate_run_started")
            self._run_started_seen = True
            return []
        if event_type == "text_delta":
            return self._text_delta(event.data)
        if event_type == "usage":
            try:
                self.usage = _usage(event.data)
            except ResponsesIngressError:
                return self.fail(code="invalid_usage")
            self._usage_seen = True
            return []
        if is_tool_event:
            return self.fail(code="unexpected_tool_event")
        if event_type == "run_error":
            return self._terminal_error(data)
        if event_type == "error":
            # Inner model/provider diagnostics are not authoritative.  The
            # canonical AgentLoop closes them with exactly one run_error.
            return []
        if event_type in {
            "approval_required",
            "side_effect_unknown",
        }:
            return self.fail(code=event_type)
        if event_type == "done":
            if self._transport_done is not None:
                return self.fail(code="duplicate_transport_done")
            self._transport_done = data
            return []
        if event_type == "run_finished":
            return self._terminal_success(data)
        return []

    def _validate_terminal_identity(
        self,
        data: dict[str, Any],
        *,
        expected_status: str | frozenset[str],
    ) -> tuple[dict[str, Any] | None, str | None]:
        envelope = data.get("terminal_envelope")
        if not isinstance(envelope, dict):
            return None, "missing_terminal_envelope"
        if self.run_id is None:
            return None, "missing_run_identity"
        expected: dict[str, str | None] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "model_id": self.model,
        }
        for field, wanted in expected.items():
            if wanted is not None and str(envelope.get(field) or "") != wanted:
                return None, "terminal_identity_mismatch"
        allowed_statuses = (
            expected_status
            if isinstance(expected_status, frozenset)
            else frozenset({expected_status})
        )
        if envelope.get("status") not in allowed_statuses:
            return None, "terminal_identity_mismatch"
        return envelope, None

    def _terminal_error(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        envelope, identity_error = self._validate_terminal_identity(
            data,
            expected_status=frozenset({"failed", "cancelled"}),
        )
        if identity_error:
            return self.fail(code=identity_error)
        assert envelope is not None
        if not isinstance(envelope.get("usage"), dict):
            return self.fail(code="missing_terminal_usage")
        try:
            self.usage = _usage(envelope.get("usage"))
        except ResponsesIngressError:
            return self.fail(code="invalid_terminal_usage")
        code = str(envelope.get("exit_reason") or "server_error")
        return self.fail(code=code, preserve_usage=True)

    def _terminal_success(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        if self._transport_done is None:
            return self.fail(code="missing_transport_done")
        envelope, identity_error = self._validate_terminal_identity(
            data,
            expected_status="succeeded",
        )
        if identity_error:
            return self.fail(code=identity_error)
        assert envelope is not None
        if not isinstance(envelope.get("usage"), dict):
            return self.fail(code="missing_terminal_usage")
        try:
            terminal_usage = _usage(envelope.get("usage"))
        except ResponsesIngressError:
            return self.fail(code="invalid_terminal_usage")
        if self._usage_seen and terminal_usage != self.usage:
            return self.fail(code="terminal_usage_mismatch")
        self.usage = terminal_usage

        total_length = self._transport_done.get("total_length")
        if (
            isinstance(total_length, bool)
            or not isinstance(total_length, int)
            or total_length < 0
            or total_length != len(self._text)
        ):
            return self.fail(code="terminal_output_mismatch")
        return self.complete()

    def complete(self) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("duplicate_terminal", status_code=500)
        if not self._text and not self.output:
            return self.fail(code="empty_response")
        events = self._close_message()
        self.terminal = True
        events.append(
            self._event("response.completed", response=self._response(status="completed"))
        )
        return events

    def fail(self, *, code: str, preserve_usage: bool = False) -> list[dict[str, Any]]:
        if self.terminal:
            raise ResponsesIngressError("duplicate_terminal", status_code=500)
        if not preserve_usage:
            self.usage = _usage({})
        events = self._close_message(status="incomplete")
        self.terminal = True
        safe_error = {
            "code": code,
            "message": "The response could not be completed.",
            "type": "server_error",
        }
        events.append(
            self._event(
                "response.failed",
                response=self._response(status="failed", error=safe_error),
            )
        )
        return events


__all__ = ["ResponsesIngressError", "ResponsesStreamProjector"]
