"""Additive Assistant run/session/turn contract helpers."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

TURN_CONTRACT_SCHEMA_VERSION = "assistant-turn-contract/v1"
TURN_KERNEL_SCHEMA_VERSION = "assistant-turn-kernel/v1"


class TurnState(str, Enum):
    """Internal attempt states; public SSE event names stay unchanged."""

    CREATED = "created"
    PREPARING = "preparing"
    MODEL_RUNNING = "model_running"
    TOOL_PENDING = "tool_pending"
    APPROVAL_PAUSED = "approval_paused"
    RECOVERY_PAUSED = "recovery_paused"
    TOOL_RUNNING = "tool_running"
    SYNTHESIZING = "synthesizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TURN_STATES = frozenset({TurnState.SUCCEEDED, TurnState.FAILED, TurnState.CANCELLED})

_ALLOWED_TURN_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.CREATED: frozenset({TurnState.PREPARING}),
    TurnState.PREPARING: frozenset(
        {
            TurnState.MODEL_RUNNING,
            TurnState.TOOL_PENDING,
            TurnState.APPROVAL_PAUSED,
            TurnState.RECOVERY_PAUSED,
            TurnState.SYNTHESIZING,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }
    ),
    TurnState.MODEL_RUNNING: frozenset(
        {
            TurnState.TOOL_PENDING,
            TurnState.APPROVAL_PAUSED,
            TurnState.RECOVERY_PAUSED,
            TurnState.SYNTHESIZING,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }
    ),
    TurnState.TOOL_PENDING: frozenset(
        {
            TurnState.APPROVAL_PAUSED,
            TurnState.RECOVERY_PAUSED,
            TurnState.TOOL_RUNNING,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }
    ),
    TurnState.APPROVAL_PAUSED: frozenset(
        {TurnState.TOOL_RUNNING, TurnState.FAILED, TurnState.CANCELLED}
    ),
    TurnState.RECOVERY_PAUSED: frozenset(
        {TurnState.TOOL_RUNNING, TurnState.FAILED, TurnState.CANCELLED}
    ),
    TurnState.TOOL_RUNNING: frozenset(
        {
            TurnState.MODEL_RUNNING,
            TurnState.APPROVAL_PAUSED,
            TurnState.RECOVERY_PAUSED,
            TurnState.SYNTHESIZING,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }
    ),
    TurnState.SYNTHESIZING: frozenset({TurnState.SUCCEEDED, TurnState.FAILED, TurnState.CANCELLED}),
    TurnState.SUCCEEDED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.CANCELLED: frozenset(),
}


class TurnTransitionError(RuntimeError):
    """An attempt tried to make an illegal state transition."""


class DuplicateTerminalError(TurnTransitionError):
    """An attempt tried to record a second terminal state."""


class FailureClass(str, Enum):
    TRANSIENT_TRANSPORT = "transient_transport"
    POLICY_UNAVAILABLE = "policy_unavailable"
    INVALID_INPUT = "invalid_input"
    PROVIDER_REFUSAL = "provider_refusal"
    SIDE_EFFECT_UNKNOWN = "side_effect_unknown"
    PERSISTENCE_FAILURE = "persistence_failure"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    MAX_ITERATIONS = "max_iterations"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"
    APPROVAL_PENDING = "approval_pending"
    RESUME_REQUIRED = "resume_required"
    COMPENSATION_REQUIRED = "compensation_required"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class RetrySafety(str, Enum):
    SAFE = "safe"
    NEEDS_IDEMPOTENCY = "needs_idempotency_or_read_back"
    UNSAFE = "unsafe"
    NOT_APPLICABLE = "not_applicable"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    DEGRADE = "degrade"
    PAUSE = "pause"
    RESUME = "resume"
    ABORT = "abort"
    COMPENSATE = "compensate"


class UserVisibility(str, Enum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class SideEffectState(str, Enum):
    NONE = "none"
    NOT_STARTED = "not_started"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureDecision:
    failure_class: FailureClass
    retry_safety: RetrySafety
    recovery_action: RecoveryAction
    user_visibility: UserVisibility
    side_effect_state: SideEffectState = SideEffectState.NONE
    recoverable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "retry_safety": self.retry_safety.value,
            "recovery_action": self.recovery_action.value,
            "user_visibility": self.user_visibility.value,
            "side_effect_state": self.side_effect_state.value,
            "recoverable": self.recoverable,
        }


def decide_failure(
    failure_class: FailureClass | str,
    *,
    side_effect_state: SideEffectState | str = SideEffectState.NONE,
) -> FailureDecision:
    """Map one stable failure class to a bounded recovery decision.

    This describes policy; it never performs a retry. An unknown write outcome
    always pauses for idempotency or read-back evidence.
    """

    try:
        normalized = FailureClass(failure_class)
    except ValueError:
        normalized = FailureClass.INTERNAL_ERROR
    try:
        side_effect = SideEffectState(side_effect_state)
    except ValueError:
        side_effect = SideEffectState.UNKNOWN

    if side_effect is SideEffectState.UNKNOWN or normalized is FailureClass.SIDE_EFFECT_UNKNOWN:
        return FailureDecision(
            normalized,
            RetrySafety.NEEDS_IDEMPOTENCY,
            RecoveryAction.PAUSE,
            UserVisibility.BLOCKING,
            SideEffectState.UNKNOWN,
            True,
        )
    if normalized in {FailureClass.TRANSIENT_TRANSPORT, FailureClass.MODEL_ERROR}:
        return FailureDecision(
            normalized,
            RetrySafety.SAFE,
            RecoveryAction.RETRY,
            UserVisibility.WARNING,
            side_effect,
            True,
        )
    if normalized is FailureClass.PROVIDER_REFUSAL:
        return FailureDecision(
            normalized,
            RetrySafety.UNSAFE,
            RecoveryAction.DEGRADE,
            UserVisibility.WARNING,
            side_effect,
            True,
        )
    if normalized is FailureClass.APPROVAL_PENDING:
        return FailureDecision(
            normalized,
            RetrySafety.NOT_APPLICABLE,
            RecoveryAction.PAUSE,
            UserVisibility.BLOCKING,
            side_effect,
            True,
        )
    if normalized is FailureClass.RESUME_REQUIRED:
        return FailureDecision(
            normalized,
            RetrySafety.NOT_APPLICABLE,
            RecoveryAction.RESUME,
            UserVisibility.BLOCKING,
            side_effect,
            True,
        )
    if normalized is FailureClass.COMPENSATION_REQUIRED:
        return FailureDecision(
            normalized,
            RetrySafety.UNSAFE,
            RecoveryAction.COMPENSATE,
            UserVisibility.BLOCKING,
            side_effect,
            True,
        )
    if normalized is FailureClass.CANCELLED:
        return FailureDecision(
            normalized,
            RetrySafety.NOT_APPLICABLE,
            RecoveryAction.ABORT,
            UserVisibility.INFO,
            side_effect,
            False,
        )
    return FailureDecision(
        normalized,
        RetrySafety.NEEDS_IDEMPOTENCY
        if normalized is FailureClass.TOOL_ERROR
        else RetrySafety.UNSAFE,
        RecoveryAction.ABORT,
        UserVisibility.WARNING
        if normalized
        in {
            FailureClass.TOOL_ERROR,
            FailureClass.MAX_ITERATIONS,
            FailureClass.RUN_BUDGET_EXCEEDED,
        }
        else UserVisibility.BLOCKING,
        side_effect,
        False,
    )


def failure_class_for_exit_reason(exit_reason: str) -> FailureClass:
    return {
        "approval_pending": FailureClass.APPROVAL_PENDING,
        "cancelled": FailureClass.CANCELLED,
        "max_iterations": FailureClass.MAX_ITERATIONS,
        "run_budget_exceeded": FailureClass.RUN_BUDGET_EXCEEDED,
        "model_error": FailureClass.MODEL_ERROR,
        "tool_error": FailureClass.TOOL_ERROR,
        "resume_required": FailureClass.RESUME_REQUIRED,
        "side_effect_unknown": FailureClass.SIDE_EFFECT_UNKNOWN,
    }.get(str(exit_reason or "").lower(), FailureClass.INTERNAL_ERROR)


def build_attempt_id(run_id: str, request_id: str, attempt_number: int) -> str:
    if not run_id or not request_id:
        raise ValueError("run_id and request_id are required")
    if attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    digest = hashlib.sha256(f"{run_id}:{request_id}:{attempt_number}".encode()).hexdigest()[:20]
    return f"att_{digest}"


@dataclass
class TurnKernel:
    """Deterministic state machine for one Assistant run attempt."""

    run_id: str
    request_id: str
    attempt_number: int = 1
    resumed_from_attempt_id: str | None = None
    state: TurnState = TurnState.CREATED
    sequence_no: int = 0
    transitions: list[dict[str, Any]] = field(default_factory=list)
    attempt_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.attempt_id = build_attempt_id(self.run_id, self.request_id, self.attempt_number)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_TURN_STATES

    def transition(self, next_state: TurnState | str, *, reason: str = "") -> dict[str, Any]:
        target = TurnState(next_state)
        if self.is_terminal:
            if target in TERMINAL_TURN_STATES:
                raise DuplicateTerminalError(
                    f"attempt {self.attempt_id} is already terminal: {self.state.value}"
                )
            raise TurnTransitionError(
                f"terminal attempt {self.attempt_id} cannot transition to {target.value}"
            )
        if target not in _ALLOWED_TURN_TRANSITIONS[self.state]:
            raise TurnTransitionError(
                f"illegal turn transition {self.state.value} -> {target.value}"
            )
        previous = self.state
        self.state = target
        self.sequence_no += 1
        transition = {
            "sequence_no": self.sequence_no,
            "from": previous.value,
            "to": target.value,
            "reason": str(reason or "")[:120],
        }
        self.transitions.append(transition)
        self.transitions = self.transitions[-32:]
        return transition

    def finish(self, status: TurnState | str, *, reason: str = "") -> dict[str, Any]:
        target = TurnState(status)
        if target not in TERMINAL_TURN_STATES:
            raise TurnTransitionError(f"{target.value} is not a terminal state")
        if self.is_terminal:
            raise DuplicateTerminalError(
                f"attempt {self.attempt_id} is already terminal: {self.state.value}"
            )
        if target is TurnState.SUCCEEDED and self.state is not TurnState.SYNTHESIZING:
            self.transition(TurnState.SYNTHESIZING, reason="terminal_synthesis")
        self.transition(target, reason=reason or "terminal")
        return self.snapshot()

    def projected(self, state: TurnState | str, *, reason: str = "") -> dict[str, Any]:
        """Return a validated state projection without committing it."""

        target = TurnState(state)
        if self.state is target:
            return self.snapshot()
        if self.is_terminal:
            raise DuplicateTerminalError(
                f"attempt {self.attempt_id} already ended as {self.state.value}"
            )
        current = self.state
        sequence = self.sequence_no
        transitions = list(self.transitions)

        def _project(next_state: TurnState, why: str) -> None:
            nonlocal current, sequence
            if next_state not in _ALLOWED_TURN_TRANSITIONS[current]:
                raise TurnTransitionError(
                    f"illegal projected turn transition {current.value} -> {next_state.value}"
                )
            sequence += 1
            transitions.append(
                {
                    "sequence_no": sequence,
                    "from": current.value,
                    "to": next_state.value,
                    "reason": str(why or "")[:120],
                }
            )
            current = next_state

        if target is TurnState.SUCCEEDED and current is not TurnState.SYNTHESIZING:
            _project(TurnState.SYNTHESIZING, "terminal_synthesis")
        _project(target, reason or "projection")
        return self._snapshot(current, sequence, transitions[-32:])

    def snapshot(self) -> dict[str, Any]:
        return self._snapshot(self.state, self.sequence_no, list(self.transitions))

    def _snapshot(
        self,
        state: TurnState,
        sequence_no: int,
        transitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": TURN_KERNEL_SCHEMA_VERSION,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "resumed_from_attempt_id": self.resumed_from_attempt_id,
            "state": state.value,
            "terminal": state in TERMINAL_TURN_STATES,
            "sequence_no": sequence_no,
            "transitions": transitions,
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str | int | float | bool):
        return enum_value
    return str(value)


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_context_snapshot(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    mode: str,
    model_id: str,
    provider: Any = None,
    trace_id: str | None = None,
    otel_trace_id: str | None = None,
    policy: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
    tools: dict[str, Any] | None = None,
    bootstrap: dict[str, Any] | None = None,
    surface: dict[str, Any] | None = None,
    attempt_id: str | None = None,
    attempt_number: int | None = None,
    turn_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, prompt-free snapshot of context compiler inputs."""

    payload: dict[str, Any] = {
        "schema_version": TURN_CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "thread_id": session_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "mode": mode,
        "model_id": model_id,
        "provider": _json_safe(provider),
        "trace_id": trace_id,
        "otel_trace_id": otel_trace_id,
        "policy": _json_safe(policy or {}),
        "memory": _json_safe(memory or {}),
        "workspace": _json_safe(workspace or {}),
        "tools": _json_safe(tools or {}),
        "bootstrap": _json_safe(bootstrap or {}),
        "surface": _json_safe(surface or {}),
    }
    if attempt_id:
        payload["attempt_id"] = attempt_id
    if attempt_number is not None:
        payload["attempt_number"] = max(1, int(attempt_number))
    if turn_state:
        payload["turn_state"] = _json_safe(turn_state)
    snapshot_hash = _stable_hash(payload)
    payload["snapshot_hash"] = snapshot_hash
    payload["snapshot_id"] = f"ctx_{snapshot_hash}"
    return payload


def build_terminal_envelope(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    mode: str,
    status: str,
    exit_reason: str,
    started_at: float,
    model_id: str,
    provider: Any = None,
    ended_at: float | None = None,
    trace_id: str | None = None,
    otel_trace_id: str | None = None,
    checkpoint_id: str | None = None,
    context_snapshot: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: Any = None,
    resume_ready: bool = False,
    approval_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    attempt_number: int | None = None,
    turn_state: dict[str, Any] | None = None,
    failure_decision: FailureDecision | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the terminal turn envelope shared by stream/non-stream paths."""

    finished_at = ended_at or time.time()
    payload: dict[str, Any] = {
        "schema_version": TURN_CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "thread_id": session_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "mode": mode,
        "status": status,
        "exit_reason": exit_reason,
        "started_at": started_at,
        "ended_at": finished_at,
        "duration_ms": max(0, int((finished_at - started_at) * 1000)),
        "model_id": model_id,
        "provider": _json_safe(provider),
        "trace_id": trace_id,
        "otel_trace_id": otel_trace_id,
        "checkpoint_id": checkpoint_id,
        "context_snapshot_id": (context_snapshot or {}).get("snapshot_id"),
        "context_snapshot": _json_safe(context_snapshot or {}),
        "usage": _json_safe(usage or {}),
        "resume_ready": bool(resume_ready),
        "approval_id": approval_id,
        "task_id": task_id,
    }
    if attempt_id:
        payload["attempt_id"] = attempt_id
    if attempt_number is not None:
        payload["attempt_number"] = max(1, int(attempt_number))
    if turn_state:
        payload["turn_state"] = _json_safe(turn_state)
    if failure_decision:
        payload["failure_decision"] = _json_safe(
            failure_decision.to_dict()
            if isinstance(failure_decision, FailureDecision)
            else failure_decision
        )
    if error:
        payload["error"] = str(error)[:500]
    return payload
