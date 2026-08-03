"""Pure reducer for the canonical Assistant turn event stream.

The collector owns no model, persistence, trace, or tool side effects.  It is
the non-stream transport adapter over the exact event producer used by SSE.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class TurnEventContractError(RuntimeError):
    """The canonical event stream violated its terminal/lifecycle contract."""


def _strictly_equal(left: Any, right: Any) -> bool:
    """Compare JSON-like payloads without Python's ``1 == True`` coercion."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _strictly_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _strictly_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


@dataclass(frozen=True)
class CollectedTurn:
    content: str
    usage: dict[str, Any]
    contexts: list[dict[str, Any]]
    tool_history: list[dict[str, Any]]
    status: str
    run_id: str | None
    session_id: str | None
    duration_ms: float
    terminal_envelope: dict[str, Any] | None
    context_snapshot: dict[str, Any] | None
    error: str | None = None
    budget_termination: dict[str, Any] | None = None
    blocked_event: dict[str, Any] | None = None


@dataclass
class TurnEventCollector:
    """Reduce projected turn events without triggering a second execution."""

    content_parts: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    contexts: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None
    session_id: str | None = None
    duration_ms: float = 0.0
    terminal_envelope: dict[str, Any] | None = None
    context_snapshot: dict[str, Any] | None = None
    budget_termination: dict[str, Any] | None = None
    blocked_event: dict[str, Any] | None = None
    _terminal_type: str | None = None
    _terminal_data: dict[str, Any] = field(default_factory=dict)
    _tool_order: list[str] = field(default_factory=list)
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    _tool_stages: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def accept(self, event: Any) -> None:
        raw_event_type = getattr(event, "event_type", "")
        event_type = str(getattr(raw_event_type, "value", raw_event_type) or "")
        data = getattr(event, "data", None)
        payload = data if isinstance(data, dict) else {}

        if self._terminal_type is not None:
            raise TurnEventContractError(
                f"event {event_type or '<empty>'} emitted after {self._terminal_type}"
            )
        if self.blocked_event is not None:
            raise TurnEventContractError(
                f"event {event_type or '<empty>'} emitted after blocked event"
            )

        self._capture_common(payload)

        if event_type == "text_delta":
            text = self._text_delta(data)
            if text:
                self.content_parts.append(text)
            return
        if event_type == "context_retrieved":
            if payload:
                self.contexts.append(dict(payload))
            return
        if event_type == "usage":
            self.usage = dict(payload)
            return
        if event_type == "done":
            # Transport close metadata is not authoritative: terminal
            # middleware may still rewrite the subsequent run_finished.
            self.duration_ms = float(payload.get("duration_ms") or self.duration_ms or 0.0)
            return
        if event_type == "run_budget_exceeded":
            self.budget_termination = dict(payload)
            return
        if event_type in {"approval_required", "side_effect_unknown"}:
            self._require_complete_tool_lifecycles(boundary=event_type)
            self._validate_boundary_contract(event_type, payload)
            self.blocked_event = {"event_type": event_type, **payload}
            return
        if event_type in {"tool_call_start", "tool_call_result", "tool_call_end"}:
            self._accept_tool_event(event_type, payload)
            return
        if event_type in {"tool_call_started", "tool_call_completed"}:
            # Legacy aliases are intentionally excluded from the canonical
            # projector. Ignore them defensively during a rolling upgrade.
            return
        if event_type in {"run_finished", "run_error"}:
            self._require_complete_tool_lifecycles(boundary=event_type)
            self._validate_boundary_contract(event_type, payload)
            self._terminal_type = event_type
            self._terminal_data = dict(payload)
            terminal_usage = self._terminal_usage(payload)
            if terminal_usage:
                self.usage = terminal_usage

    def finalize(self) -> CollectedTurn:
        self._require_complete_tool_lifecycles(boundary="stream end")
        if self._terminal_type is None and self.blocked_event is None:
            raise TurnEventContractError("turn stream ended without terminal or blocked event")

        if self._terminal_type == "run_finished":
            status = "succeeded"
            error = None
        elif self._terminal_type == "run_error":
            envelope_status = str((self.terminal_envelope or {}).get("status") or "")
            status = "cancelled" if envelope_status == "cancelled" else "failed"
            error = str(
                self._terminal_data.get("error")
                or self._terminal_data.get("message")
                or "assistant_run_failed"
            )
        else:
            status = "blocked"
            error = None

        return CollectedTurn(
            content="".join(self.content_parts),
            usage=dict(self.usage),
            contexts=list(self.contexts),
            tool_history=[dict(self._tools[tool_id]) for tool_id in self._tool_order],
            status=status,
            run_id=self.run_id,
            session_id=self.session_id,
            duration_ms=self.duration_ms,
            terminal_envelope=(
                dict(self.terminal_envelope) if self.terminal_envelope is not None else None
            ),
            context_snapshot=(
                dict(self.context_snapshot) if self.context_snapshot is not None else None
            ),
            error=error,
            budget_termination=(
                dict(self.budget_termination) if self.budget_termination is not None else None
            ),
            blocked_event=(dict(self.blocked_event) if self.blocked_event is not None else None),
        )

    def _capture_common(self, payload: dict[str, Any]) -> None:
        self._capture_identity(payload, source="event")
        envelope = payload.get("terminal_envelope")
        snapshot = payload.get("context_snapshot")
        if isinstance(envelope, dict):
            self._capture_identity(envelope, source="terminal_envelope")
            self.terminal_envelope = dict(envelope)
        if isinstance(snapshot, dict):
            self._capture_identity(snapshot, source="context_snapshot")
            self.context_snapshot = dict(snapshot)

    def _capture_identity(self, payload: dict[str, Any], *, source: str) -> None:
        raw_run_id = payload.get("run_id")
        raw_session_id = payload.get("session_id")
        raw_thread_id = payload.get("thread_id")
        if (
            raw_session_id is not None
            and raw_thread_id is not None
            and not _strictly_equal(raw_session_id, raw_thread_id)
        ):
            raise TurnEventContractError(f"{source} session_id/thread_id mismatch")
        raw_session = raw_session_id if raw_session_id is not None else raw_thread_id
        self.run_id = self._lock_identity(
            field_name="run_id",
            current=self.run_id,
            incoming=raw_run_id,
            source=source,
        )
        self.session_id = self._lock_identity(
            field_name="session_id",
            current=self.session_id,
            incoming=raw_session,
            source=source,
        )

    @staticmethod
    def _lock_identity(
        *,
        field_name: str,
        current: str | None,
        incoming: Any,
        source: str,
    ) -> str | None:
        if incoming is None:
            return current
        if not isinstance(incoming, str) or not incoming:
            raise TurnEventContractError(f"{source} {field_name} must be a non-empty string")
        if current is not None and current != incoming:
            raise TurnEventContractError(
                f"{field_name} changed from {current} to {incoming} in {source}"
            )
        return incoming

    def _validate_boundary_contract(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        expected_statuses = {
            "run_finished": {"succeeded"},
            "run_error": {"failed", "cancelled"},
            "approval_required": {"blocked"},
            "side_effect_unknown": {"blocked"},
        }[event_type]
        expected_states = {
            "run_finished": {"succeeded"},
            "run_error": {"failed", "cancelled"},
            "approval_required": {"approval_paused"},
            "side_effect_unknown": {"recovery_paused"},
        }[event_type]
        envelope = payload.get("terminal_envelope")
        if envelope is not None and not isinstance(envelope, dict):
            raise TurnEventContractError("terminal_envelope must be an object")
        if isinstance(envelope, dict):
            status = envelope.get("status")
            if status not in expected_statuses:
                raise TurnEventContractError(
                    f"{event_type} conflicts with terminal_envelope status {status!r}"
                )

        event_turn_state = payload.get("turn_state")
        envelope_turn_state = envelope.get("turn_state") if isinstance(envelope, dict) else None
        for source, turn_state in (
            ("event", event_turn_state),
            ("terminal_envelope", envelope_turn_state),
        ):
            if turn_state is not None and not isinstance(turn_state, dict):
                raise TurnEventContractError(f"{source} turn_state must be an object")
        if (
            event_turn_state is not None
            and envelope_turn_state is not None
            and not _strictly_equal(event_turn_state, envelope_turn_state)
        ):
            raise TurnEventContractError("event and terminal_envelope turn_state mismatch")
        turn_state = event_turn_state or envelope_turn_state
        if isinstance(turn_state, dict):
            state = turn_state.get("state")
            if state not in expected_states:
                raise TurnEventContractError(
                    f"{event_type} conflicts with turn_state state {state!r}"
                )
            expected_terminal = event_type in {"run_finished", "run_error"}
            terminal = turn_state.get("terminal")
            if terminal is not None and (
                not isinstance(terminal, bool) or terminal is not expected_terminal
            ):
                raise TurnEventContractError(
                    f"{event_type} conflicts with turn_state terminal {terminal!r}"
                )
            self._capture_identity(turn_state, source="turn_state")

    def _accept_tool_event(self, event_type: str, payload: dict[str, Any]) -> None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id:
            raise TurnEventContractError(f"{event_type} missing tool_call_id")
        if tool_call_id not in self._tools:
            self._tool_order.append(tool_call_id)
            self._tools[tool_call_id] = {"tool_call_id": tool_call_id}
            self._tool_stages[tool_call_id] = {}
        stage = event_type.removeprefix("tool_call_")
        stages = self._tool_stages[tool_call_id]
        if stage in stages:
            if not _strictly_equal(stages[stage], payload):
                raise TurnEventContractError(f"conflicting replay for {tool_call_id} stage {stage}")
            return
        expected_stage = ("start", "result", "end")[len(stages)]
        if stage != expected_stage:
            raise TurnEventContractError(
                f"tool {tool_call_id} expected {expected_stage}, received {stage}"
            )
        stage_payload = copy.deepcopy(payload)
        stages[stage] = stage_payload
        record = self._tools[tool_call_id]
        record.update(stage_payload)
        record[stage] = stage_payload
        record["stages"] = list(stages)

    def _require_complete_tool_lifecycles(self, *, boundary: str) -> None:
        for tool_call_id in self._tool_order:
            stages = self._tool_stages[tool_call_id]
            if tuple(stages) != ("start", "result", "end"):
                raise TurnEventContractError(
                    f"tool {tool_call_id} lifecycle incomplete before {boundary}"
                )

    @staticmethod
    def _text_delta(data: Any) -> str:
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("content", "delta", "text"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
        return ""

    @staticmethod
    def _terminal_usage(payload: dict[str, Any]) -> dict[str, Any]:
        envelope = payload.get("terminal_envelope")
        if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict):
            return dict(envelope["usage"])
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("usage"), dict):
            return dict(metadata["usage"])
        return {}
