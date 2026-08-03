"""Hard, resumable resource budgets for one Assistant run.

The budget is deliberately provider- and transport-agnostic.  Streaming and
non-streaming callers consume the same event producer, while approval resume
restores this snapshot so a new attempt cannot silently expand the original
run's limits.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

RUN_BUDGET_SCHEMA_VERSION = "assistant-run-budget/v1"

_LIMIT_KEYS = frozenset(
    {
        "max_model_turns",
        "max_tool_calls",
        "max_parallel_tool_calls",
        "max_wall_time_seconds",
        "max_tool_result_bytes",
    }
)
_USAGE_KEYS = frozenset(
    {
        "model_turns",
        "tool_calls",
        "tool_result_bytes",
        "elapsed_ms",
    }
)
_REMAINING_KEYS = frozenset(
    {
        "model_turns",
        "tool_calls",
        "tool_result_bytes",
        "wall_time_ms",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {
        "schema_version",
        "limits",
        "usage",
        "remaining",
        "exhausted",
        "reason",
    }
)


def _require_exact_keys(payload: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if frozenset(payload) != expected:
        raise ValueError(f"invalid {label} fields")


def _require_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class RunBudgetDimension(str, Enum):
    MODEL_TURNS = "model_turns"
    TOOL_CALLS = "tool_calls"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    WALL_TIME = "wall_time"
    TOOL_RESULT_BYTES = "tool_result_bytes"


@dataclass(frozen=True)
class RunBudgetLimits:
    """Immutable upper bounds for one logical run, across resume attempts."""

    max_model_turns: int
    max_tool_calls: int
    max_parallel_tool_calls: int
    max_wall_time_seconds: float
    max_tool_result_bytes: int

    def __post_init__(self) -> None:
        integer_limits = {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_parallel_tool_calls": self.max_parallel_tool_calls,
            "max_tool_result_bytes": self.max_tool_result_bytes,
        }
        for name, value in integer_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        wall_time = self.max_wall_time_seconds
        if not _is_finite_number(wall_time) or wall_time <= 0:
            raise ValueError("max_wall_time_seconds must be positive and finite")

    @classmethod
    def from_legacy(
        cls,
        *,
        max_tool_iterations: int,
        max_concurrent_tools: int,
    ) -> RunBudgetLimits:
        """Map existing AgentLoop knobs without tightening legacy behavior."""

        if isinstance(max_tool_iterations, bool) or not isinstance(max_tool_iterations, int):
            raise ValueError("max_tool_iterations must be an integer")
        if isinstance(max_concurrent_tools, bool) or not isinstance(max_concurrent_tools, int):
            raise ValueError("max_concurrent_tools must be an integer")
        iterations = max(1, max_tool_iterations)
        concurrent = max(1, max_concurrent_tools)
        return cls(
            # Main turns plus the two existing forced-synthesis fallbacks.
            max_model_turns=iterations + 2,
            max_tool_calls=iterations * concurrent,
            max_parallel_tool_calls=concurrent,
            max_wall_time_seconds=300.0,
            # Existing per-result caps are much smaller.  This cumulative cap
            # remains a compatibility ceiling while making growth finite.
            max_tool_result_bytes=256_000,
        )

    def bounded_by(self, prior: RunBudgetLimits) -> RunBudgetLimits:
        """Return limits that can only stay equal or shrink on resume."""

        return RunBudgetLimits(
            max_model_turns=min(self.max_model_turns, prior.max_model_turns),
            max_tool_calls=min(self.max_tool_calls, prior.max_tool_calls),
            max_parallel_tool_calls=min(
                self.max_parallel_tool_calls,
                prior.max_parallel_tool_calls,
            ),
            max_wall_time_seconds=min(
                self.max_wall_time_seconds,
                prior.max_wall_time_seconds,
            ),
            max_tool_result_bytes=min(
                self.max_tool_result_bytes,
                prior.max_tool_result_bytes,
            ),
        )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_parallel_tool_calls": self.max_parallel_tool_calls,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_tool_result_bytes": self.max_tool_result_bytes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RunBudgetLimits:
        if not isinstance(payload, dict):
            raise ValueError("run budget limits must be an object")
        _require_exact_keys(payload, _LIMIT_KEYS, "run budget limits")
        return cls(
            max_model_turns=payload["max_model_turns"],
            max_tool_calls=payload["max_tool_calls"],
            max_parallel_tool_calls=payload["max_parallel_tool_calls"],
            max_wall_time_seconds=payload["max_wall_time_seconds"],
            max_tool_result_bytes=payload["max_tool_result_bytes"],
        )


class RunBudgetExceeded(RuntimeError):
    """A hard budget dimension refused additional work."""

    def __init__(
        self,
        *,
        dimension: RunBudgetDimension,
        limit: int | float,
        used: int | float,
        requested: int | float,
        snapshot: dict[str, Any],
    ) -> None:
        self.dimension = dimension
        self.reason = f"{dimension.value}_exhausted"
        self.limit = limit
        self.used = used
        self.requested = requested
        self.snapshot = snapshot
        super().__init__(self.reason)

    def to_event_data(self) -> dict[str, Any]:
        return {
            "schema_version": RUN_BUDGET_SCHEMA_VERSION,
            "status": "exhausted",
            "reason": self.reason,
            "dimension": self.dimension.value,
            "limit": self.limit,
            "used": self.used,
            "requested": self.requested,
            "budget": self.snapshot,
        }


@dataclass
class RunBudget:
    """Mutable counters enforcing immutable limits for one logical run."""

    limits: RunBudgetLimits
    model_turns: int = 0
    tool_calls: int = 0
    tool_result_bytes: int = 0
    elapsed_before_resume_seconds: float = 0.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _started_at: float = field(init=False, repr=False)
    _exhausted: RunBudgetExceeded | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.limits, RunBudgetLimits):
            raise ValueError("limits must be RunBudgetLimits")
        for name in ("model_turns", "tool_calls", "tool_result_bytes"):
            value = getattr(self, name)
            setattr(self, name, _require_non_negative_int(value, name))
        elapsed = self.elapsed_before_resume_seconds
        if not _is_finite_number(elapsed) or elapsed < 0:
            raise ValueError("elapsed_before_resume_seconds must be finite and non-negative")
        self.elapsed_before_resume_seconds = float(elapsed)
        started_at = self.clock()
        if not _is_finite_number(started_at):
            raise ValueError("clock must return a finite number")
        self._started_at = float(started_at)

    @property
    def elapsed_seconds(self) -> float:
        return self.elapsed_before_resume_seconds + max(0.0, self.clock() - self._started_at)

    @property
    def exhausted(self) -> bool:
        return self._exhausted is not None

    @property
    def remaining_wall_time_seconds(self) -> float:
        self.check_wall_time()
        return max(0.0, self.limits.max_wall_time_seconds - self.elapsed_seconds)

    def consume_model_turn(self) -> None:
        self.check_wall_time()
        requested = self.model_turns + 1
        if requested > self.limits.max_model_turns:
            self._raise_exhausted(
                RunBudgetDimension.MODEL_TURNS,
                limit=self.limits.max_model_turns,
                used=self.model_turns,
                requested=requested,
            )
        self.model_turns = requested

    def reserve_tool_batch(self, count: int) -> None:
        self.check_wall_time()
        count = _require_non_negative_int(count, "tool batch count")
        if count > self.limits.max_parallel_tool_calls:
            self._raise_exhausted(
                RunBudgetDimension.PARALLEL_TOOL_CALLS,
                limit=self.limits.max_parallel_tool_calls,
                used=0,
                requested=count,
            )
        requested = self.tool_calls + count
        if requested > self.limits.max_tool_calls:
            self._raise_exhausted(
                RunBudgetDimension.TOOL_CALLS,
                limit=self.limits.max_tool_calls,
                used=self.tool_calls,
                requested=requested,
            )
        self.tool_calls = requested

    def observe_tool_result(self, value: Any) -> int:
        self.check_wall_time()
        size = self._encoded_size(value)
        requested = self.tool_result_bytes + size
        if requested > self.limits.max_tool_result_bytes:
            self._raise_exhausted(
                RunBudgetDimension.TOOL_RESULT_BYTES,
                limit=self.limits.max_tool_result_bytes,
                used=self.tool_result_bytes,
                requested=requested,
            )
        self.tool_result_bytes = requested
        return size

    def check_wall_time(self) -> None:
        if self._exhausted is not None:
            raise self._exhausted
        elapsed = self.elapsed_seconds
        if elapsed > self.limits.max_wall_time_seconds:
            self._raise_exhausted(
                RunBudgetDimension.WALL_TIME,
                limit=self.limits.max_wall_time_seconds,
                used=elapsed,
                requested=elapsed,
            )

    def exhaust_wall_time(self) -> None:
        elapsed = max(self.elapsed_seconds, self.limits.max_wall_time_seconds)
        self._raise_exhausted(
            RunBudgetDimension.WALL_TIME,
            limit=self.limits.max_wall_time_seconds,
            used=elapsed,
            requested=elapsed,
        )

    def snapshot(self) -> dict[str, Any]:
        # Round usage up so every checkpoint is conservative: repeated
        # pause/resume cycles can never refund the sub-millisecond fraction.
        elapsed_ms = max(0, math.ceil(self.elapsed_seconds * 1000))
        wall_time_limit_ms = math.ceil(self.limits.max_wall_time_seconds * 1000)
        return {
            "schema_version": RUN_BUDGET_SCHEMA_VERSION,
            "limits": self.limits.to_dict(),
            "usage": {
                "model_turns": self.model_turns,
                "tool_calls": self.tool_calls,
                "tool_result_bytes": self.tool_result_bytes,
                "elapsed_ms": elapsed_ms,
            },
            "remaining": {
                "model_turns": max(0, self.limits.max_model_turns - self.model_turns),
                "tool_calls": max(0, self.limits.max_tool_calls - self.tool_calls),
                "tool_result_bytes": max(
                    0,
                    self.limits.max_tool_result_bytes - self.tool_result_bytes,
                ),
                "wall_time_ms": max(
                    0,
                    wall_time_limit_ms - elapsed_ms,
                ),
            },
            "exhausted": self._exhausted is not None,
            "reason": self._exhausted.reason if self._exhausted is not None else None,
        }

    @classmethod
    def restore(
        cls,
        *,
        configured_limits: RunBudgetLimits,
        snapshot: dict[str, Any] | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> RunBudget:
        """Strictly restore a persisted approval-run budget.

        Initial runs construct :class:`RunBudget` directly.  This method is
        deliberately fail-closed: missing, drifted, or internally
        inconsistent snapshots are never replaced with a fresh budget.
        """

        if not isinstance(snapshot, dict):
            raise ValueError("run budget snapshot is required")
        _require_exact_keys(snapshot, _SNAPSHOT_KEYS, "run budget snapshot")
        if snapshot["schema_version"] != RUN_BUDGET_SCHEMA_VERSION:
            raise ValueError("unsupported run budget snapshot schema")
        prior_limits_raw = snapshot["limits"]
        usage = snapshot["usage"]
        remaining = snapshot["remaining"]
        if not isinstance(prior_limits_raw, dict):
            raise ValueError("run budget limits must be an object")
        if not isinstance(usage, dict):
            raise ValueError("run budget usage must be an object")
        if not isinstance(remaining, dict):
            raise ValueError("run budget remaining must be an object")
        prior_limits = RunBudgetLimits.from_dict(prior_limits_raw)
        if any(
            getattr(prior_limits, field_name) > getattr(configured_limits, field_name)
            for field_name in _LIMIT_KEYS
        ):
            raise ValueError("persisted run budget limits exceed configured limits")
        _require_exact_keys(usage, _USAGE_KEYS, "run budget usage")
        model_turns = _require_non_negative_int(usage["model_turns"], "model_turns")
        tool_calls = _require_non_negative_int(usage["tool_calls"], "tool_calls")
        tool_result_bytes = _require_non_negative_int(
            usage["tool_result_bytes"],
            "tool_result_bytes",
        )
        elapsed_ms = _require_non_negative_int(usage["elapsed_ms"], "elapsed_ms")
        if model_turns > prior_limits.max_model_turns:
            raise ValueError("model_turns exceeds persisted limit")
        if tool_calls > prior_limits.max_tool_calls:
            raise ValueError("tool_calls exceeds persisted limit")
        if tool_result_bytes > prior_limits.max_tool_result_bytes:
            raise ValueError("tool_result_bytes exceeds persisted limit")
        wall_time_limit_ms = math.ceil(prior_limits.max_wall_time_seconds * 1000)
        if elapsed_ms > wall_time_limit_ms:
            raise ValueError("elapsed_ms exceeds persisted wall-time limit")

        exhausted = snapshot["exhausted"]
        if not isinstance(exhausted, bool):
            raise ValueError("run budget exhausted must be boolean")
        if exhausted or snapshot["reason"] is not None:
            raise ValueError("exhausted run budget cannot be resumed")

        _require_exact_keys(remaining, _REMAINING_KEYS, "run budget remaining")
        normalized_remaining = {
            key: _require_non_negative_int(value, f"remaining.{key}")
            for key, value in remaining.items()
        }
        expected_remaining = {
            "model_turns": prior_limits.max_model_turns - model_turns,
            "tool_calls": prior_limits.max_tool_calls - tool_calls,
            "tool_result_bytes": prior_limits.max_tool_result_bytes - tool_result_bytes,
            "wall_time_ms": wall_time_limit_ms - elapsed_ms,
        }
        if normalized_remaining != expected_remaining:
            raise ValueError("run budget remaining counters are inconsistent")

        return cls(
            prior_limits,
            model_turns=model_turns,
            tool_calls=tool_calls,
            tool_result_bytes=tool_result_bytes,
            elapsed_before_resume_seconds=elapsed_ms / 1000,
            clock=clock,
        )

    @staticmethod
    def _encoded_size(value: Any) -> int:
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, str):
            return len(value.encode("utf-8"))
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return len(encoded)

    def _raise_exhausted(
        self,
        dimension: RunBudgetDimension,
        *,
        limit: int | float,
        used: int | float,
        requested: int | float,
    ) -> None:
        if self._exhausted is None:
            # Build the snapshot before attaching the exception to avoid a
            # recursive snapshot -> exception -> snapshot cycle.
            snapshot = self.snapshot()
            self._exhausted = RunBudgetExceeded(
                dimension=dimension,
                limit=limit,
                used=used,
                requested=requested,
                snapshot=snapshot,
            )
            self._exhausted.snapshot = self.snapshot()
        raise self._exhausted
