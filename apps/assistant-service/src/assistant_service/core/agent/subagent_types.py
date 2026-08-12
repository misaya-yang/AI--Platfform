"""
Sub-Agent Type Definitions — ADR-003.

Defines agent types (explore/task/plan), their configs, runtime state,
and default parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .subagent_output_contract import normalize_output_schema


class SubAgentType(str, Enum):
    EXPLORE = "explore"  # Fast search, read-only tools
    TASK = "task"  # Full execution with all tools
    PLAN = "plan"  # Analyze and output a plan


@dataclass
class SubAgentAdaptiveBudget:
    """Small progress gate for extending a child's recommended limits."""

    initial_turns: int
    initial_tool_calls: int
    initial_timeout_seconds: float
    max_turns: int
    max_tool_calls: int
    hard_timeout_seconds: float
    idle_timeout_seconds: float
    turn_step: int = 2
    tool_call_step: int = 4
    timeout_step_seconds: float = 60.0
    max_stagnant_steps: int = 2
    effective_turns: int = field(init=False)
    effective_tool_calls: int = field(init=False)
    effective_timeout_seconds: float = field(init=False)
    extensions: int = 0
    last_progress_at: float = 0.0
    stagnant_steps: int = 0
    consecutive_failures: int = 0
    stop_reason: str | None = None
    novel_progress: int = 0
    consumed_progress: int = 0
    _seen_progress: set[str] = field(default_factory=set, repr=False)
    _last_failure: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        self.effective_turns = min(self.initial_turns, self.max_turns)
        self.effective_tool_calls = min(self.initial_tool_calls, self.max_tool_calls)
        self.effective_timeout_seconds = min(
            self.initial_timeout_seconds,
            self.hard_timeout_seconds,
        )

    def note_progress(self, fingerprint: str, *, now: float) -> bool:
        """Record novel evidence; extension is granted only when quota is reached."""

        if not fingerprint or fingerprint in self._seen_progress:
            self.stagnant_steps += 1
            if self.stagnant_steps >= self.max_stagnant_steps:
                self.stop_reason = "repeated_or_no_progress"
            return False
        self._seen_progress.add(fingerprint)
        self.last_progress_at = now
        self.stagnant_steps = 0
        self.consecutive_failures = 0
        self._last_failure = ""
        self.novel_progress += 1
        return True

    def extend_if_needed(self, *, turns: int, tool_calls: int, now: float) -> bool:
        """Grant one small lease only at a reached quota with recent progress."""

        if (
            self.stop_reason is not None
            or self.novel_progress <= self.consumed_progress
            or now - self.last_progress_at >= self.idle_timeout_seconds
            or (turns < self.effective_turns and tool_calls < self.effective_tool_calls)
        ):
            return False
        before = (
            self.effective_turns,
            self.effective_tool_calls,
            self.effective_timeout_seconds,
        )
        self.effective_turns = min(self.max_turns, self.effective_turns + self.turn_step)
        self.effective_tool_calls = min(
            self.max_tool_calls,
            self.effective_tool_calls + self.tool_call_step,
        )
        self.effective_timeout_seconds = min(
            self.hard_timeout_seconds,
            self.effective_timeout_seconds + self.timeout_step_seconds,
        )
        changed = before != (
            self.effective_turns,
            self.effective_tool_calls,
            self.effective_timeout_seconds,
        )
        if changed:
            self.extensions += 1
            self.consumed_progress += 1
        return changed

    def note_failure(self, fingerprint: str) -> None:
        repeated = bool(fingerprint and fingerprint == self._last_failure)
        self._last_failure = fingerprint
        self.consecutive_failures = self.consecutive_failures + 1 if repeated else 1
        self.stagnant_steps = self.stagnant_steps + 1 if repeated else 1
        if self.consecutive_failures >= self.max_stagnant_steps:
            self.stop_reason = "consecutive_tool_failures"
        elif self.stagnant_steps >= self.max_stagnant_steps:
            self.stop_reason = "repeated_or_no_progress"

    def timed_out(self, *, now: float, started_at: float) -> bool:
        reason = self.timeout_reason(now=now, started_at=started_at)
        if reason is not None:
            self.stop_reason = reason
        return reason is not None

    def timeout_reason(self, *, now: float, started_at: float) -> str | None:
        progress_reference = max(self.last_progress_at, started_at)
        if now - started_at >= self.hard_timeout_seconds:
            return "hard_timeout"
        if now - progress_reference >= self.idle_timeout_seconds:
            return "idle_timeout"
        return None

    def operation_deadline(self, *, started_at: float) -> float:
        """Return the nearest host timeout in the injected monotonic domain."""

        progress_reference = max(self.last_progress_at, started_at)
        return min(
            started_at + self.effective_timeout_seconds,
            progress_reference + self.idle_timeout_seconds,
            started_at + self.hard_timeout_seconds,
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "initial": {
                "max_turns": self.initial_turns,
                "max_tool_calls": self.initial_tool_calls,
                "timeout_seconds": self.initial_timeout_seconds,
            },
            "effective": {
                "max_turns": self.effective_turns,
                "max_tool_calls": self.effective_tool_calls,
                "timeout_seconds": self.effective_timeout_seconds,
            },
            "hard_ceiling": {
                "max_turns": self.max_turns,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.hard_timeout_seconds,
            },
            "extensions": self.extensions,
            "stop_reason": self.stop_reason,
        }


@dataclass
class SubAgentConfig:
    """Configuration for spawning a sub-agent."""

    agent_type: SubAgentType
    prompt: str
    description: str = ""

    # Execution limits
    max_turns: int = 10
    max_tool_calls: int = 20
    max_tokens: int = 4096
    timeout_seconds: int = 120
    idle_timeout_seconds: int | None = None
    adaptive_budget: bool = False

    # Model override (None = use default for agent type)
    model_override: str | None = None

    # Context from parent agent
    parent_context: str | None = None
    include_history: bool = False

    # Optional host-enforced structured result. The schema is carried through
    # the internal marker but grants no additional authority or tool access.
    output_schema: dict[str, Any] | None = None

    # Optional plugin-defined specialist profile.  These values are an
    # additional restriction layered below the parent authority and the
    # built-in agent-type defaults; they can never grant a capability.
    profile_id: str | None = None
    profile_name: str | None = None
    profile_instructions: str | None = None
    allowed_tools: frozenset[str] | None = None
    allowed_tool_categories: frozenset[str] | None = None
    definition_sha256: str | None = None
    source_plugin: str | None = None

    # Stable input position for a bounded parallel dispatch.  It is emitted
    # on every child event so consumers do not have to infer ordering from
    # completion time.
    dispatch_index: int | None = None

    # Stable logical identity and operator-derived lineage.  The model may
    # propose ids, but the host validates or deterministically derives them;
    # depth/lineage are always rebound from the parent invocation context.
    delegation_id: str = ""
    task_id: str = ""
    parent_task_id: str | None = None
    lineage: tuple[str, ...] = ()
    depth: int = 0

    def to_marker(self) -> dict[str, Any]:
        """Return the JSON-safe host marker persisted by the execution gateway."""

        return {
            "agent_type": self.agent_type.value,
            "prompt": self.prompt,
            "description": self.description,
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "adaptive_budget": self.adaptive_budget,
            "model_override": self.model_override,
            "parent_context": self.parent_context,
            "include_history": self.include_history,
            "output_schema": normalize_output_schema(self.output_schema),
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "profile_instructions": self.profile_instructions,
            "allowed_tools": (
                sorted(self.allowed_tools) if self.allowed_tools is not None else None
            ),
            "allowed_tool_categories": (
                sorted(self.allowed_tool_categories)
                if self.allowed_tool_categories is not None
                else None
            ),
            "definition_sha256": self.definition_sha256,
            "source_plugin": self.source_plugin,
            "dispatch_index": self.dispatch_index,
            "delegation_id": self.delegation_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "lineage": list(self.lineage),
            "depth": self.depth,
        }

    @classmethod
    def from_marker(cls, value: Any) -> SubAgentConfig:
        """Validate and restore an internal JSON marker before child execution."""

        if isinstance(value, cls):
            # Rolling-upgrade compatibility for an in-flight pre-v1 marker.
            return value
        if not isinstance(value, dict):
            raise ValueError("sub-agent config marker must be an object")
        try:
            agent_type = SubAgentType(str(value["agent_type"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("sub-agent config marker has an invalid agent_type") from exc
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("sub-agent config marker requires a non-empty prompt")

        integer_fields: dict[str, int] = {}
        for name in ("max_turns", "max_tool_calls", "max_tokens", "timeout_seconds"):
            item = value.get(name)
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise ValueError(f"sub-agent config marker has an invalid {name}")
            integer_fields[name] = item
        idle_timeout_seconds = value.get("idle_timeout_seconds")
        if idle_timeout_seconds is not None and (
            isinstance(idle_timeout_seconds, bool)
            or not isinstance(idle_timeout_seconds, int)
            or idle_timeout_seconds <= 0
        ):
            raise ValueError("sub-agent config marker has an invalid idle_timeout_seconds")

        def _string(name: str) -> str | None:
            item = value.get(name)
            if item is None:
                return None
            if not isinstance(item, str):
                raise ValueError(f"sub-agent config marker has an invalid {name}")
            return item

        def _string_set(name: str) -> frozenset[str] | None:
            item = value.get(name)
            if item is None:
                return None
            if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
                raise ValueError(f"sub-agent config marker has an invalid {name}")
            return frozenset(item)

        dispatch_index = value.get("dispatch_index")
        if dispatch_index is not None and (
            isinstance(dispatch_index, bool)
            or not isinstance(dispatch_index, int)
            or dispatch_index < 0
        ):
            raise ValueError("sub-agent config marker has an invalid dispatch_index")
        include_history = value.get("include_history", False)
        if not isinstance(include_history, bool):
            raise ValueError("sub-agent config marker has an invalid include_history")
        adaptive_budget = value.get("adaptive_budget", False)
        if not isinstance(adaptive_budget, bool):
            raise ValueError("sub-agent config marker has an invalid adaptive_budget")
        lineage = value.get("lineage", [])
        if not isinstance(lineage, list) or any(
            not isinstance(item, str) or not item for item in lineage
        ):
            raise ValueError("sub-agent config marker has an invalid lineage")
        depth = value.get("depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("sub-agent config marker has an invalid depth")

        return cls(
            agent_type=agent_type,
            prompt=prompt,
            description=_string("description") or "",
            **integer_fields,
            idle_timeout_seconds=idle_timeout_seconds,
            adaptive_budget=adaptive_budget,
            model_override=_string("model_override"),
            parent_context=_string("parent_context"),
            include_history=include_history,
            output_schema=normalize_output_schema(value.get("output_schema")),
            profile_id=_string("profile_id"),
            profile_name=_string("profile_name"),
            profile_instructions=_string("profile_instructions"),
            allowed_tools=_string_set("allowed_tools"),
            allowed_tool_categories=_string_set("allowed_tool_categories"),
            definition_sha256=_string("definition_sha256"),
            source_plugin=_string("source_plugin"),
            dispatch_index=dispatch_index,
            delegation_id=_string("delegation_id") or "",
            task_id=_string("task_id") or "",
            parent_task_id=_string("parent_task_id"),
            lineage=tuple(lineage),
            depth=depth,
        )


@dataclass
class SubAgentStep:
    """A single tool call within a sub-agent execution."""

    tool_name: str
    call_id: str
    status: str = "running"  # running | completed | failed
    summary: str | None = None
    duration_ms: float = 0


@dataclass
class SubAgentState:
    """Runtime state of a sub-agent."""

    agent_id: str
    agent_type: SubAgentType
    description: str
    dispatch_index: int | None = None
    profile_id: str | None = None
    profile_name: str | None = None
    definition_sha256: str | None = None
    source_plugin: str | None = None
    delegation_id: str = ""
    task_id: str = ""
    parent_task_id: str | None = None
    lineage: tuple[str, ...] = ()
    depth: int = 0
    status: str = "pending"  # pending | running | completed | failed | cancelled

    # Progress
    current_step: str = ""
    turns_completed: int = 0
    tool_calls_made: int = 0

    # Result
    result: str | None = None
    error: str | None = None
    structured_payload: dict[str, Any] | None = None
    structured_validation_errors: list[str] = field(default_factory=list)
    structured_correction_rounds: int = 0

    # Timing
    started_at: float | None = None
    finished_at: float | None = None
    started_monotonic_ms: float | None = None
    finished_monotonic_ms: float | None = None
    duration_ms: float | None = None

    # Effective execution receipt values (after parent/profile narrowing).
    effective_model_id: str = ""
    effective_tool_names: tuple[str, ...] = ()
    effective_tool_categories: tuple[str, ...] = ()
    effective_limits: dict[str, int | float] = field(default_factory=dict)
    initial_limits: dict[str, int | float] = field(default_factory=dict)
    hard_limits: dict[str, int | float] = field(default_factory=dict)
    budget_extensions: int = 0
    budget_stop_reason: str | None = None

    # Steps (for frontend timeline)
    steps: list[SubAgentStep] = field(default_factory=list)


# Default configs per agent type
SUBAGENT_DEFAULTS: dict[SubAgentType, dict[str, Any]] = {
    SubAgentType.EXPLORE: {
        "initial_max_turns": 8,
        "initial_max_tool_calls": 15,
        "initial_timeout_seconds": 120,
        "max_turns": 16,
        "max_tool_calls": 32,
        "max_tokens": 4096,
        "timeout_seconds": 600,
        "idle_timeout_seconds": 120,
        "allowed_tool_categories": {"retrieval", "utility"},
        "system_prompt_suffix": (
            "You are an Explore agent. Find the requested information with the provided "
            "read-only tools and report the evidence concisely."
        ),
    },
    SubAgentType.TASK: {
        "initial_max_turns": 10,
        "initial_max_tool_calls": 20,
        "initial_timeout_seconds": 180,
        "max_turns": 24,
        "max_tool_calls": 48,
        "max_tokens": 4096,
        "timeout_seconds": 900,
        "idle_timeout_seconds": 180,
        "allowed_tool_categories": None,  # All tools
        "system_prompt_suffix": (
            "You are a Task agent. Complete the assigned task with relevant provided tools "
            "and return the observed result clearly."
        ),
    },
    SubAgentType.PLAN: {
        "initial_max_turns": 5,
        "initial_max_tool_calls": 10,
        "initial_timeout_seconds": 120,
        "max_turns": 10,
        "max_tool_calls": 20,
        "max_tokens": 4096,
        "timeout_seconds": 300,
        "idle_timeout_seconds": 120,
        "allowed_tool_categories": {"retrieval", "utility"},
        "system_prompt_suffix": (
            "You are a Plan agent. Analyze the request and design a clear "
            "step-by-step implementation plan. Return structured instructions."
        ),
    },
}
