"""
Sub-Agent Type Definitions — ADR-003.

Defines agent types (explore/task/plan), their configs, runtime state,
and default parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SubAgentType(str, Enum):
    EXPLORE = "explore"  # Fast search, read-only tools
    TASK = "task"  # Full execution with all tools
    PLAN = "plan"  # Analyze and output a plan


@dataclass
class SubAgentConfig:
    """Configuration for spawning a sub-agent."""

    agent_type: SubAgentType
    prompt: str
    description: str = ""

    # Execution limits
    max_turns: int = 10
    max_tool_calls: int = 20
    max_tokens: int = 2048
    timeout_seconds: int = 120

    # Model override (None = use default for agent type)
    model_override: str | None = None

    # Context from parent agent
    parent_context: str | None = None
    include_history: bool = False


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
    status: str = "pending"  # pending | running | completed | failed | cancelled

    # Progress
    current_step: str = ""
    turns_completed: int = 0
    tool_calls_made: int = 0

    # Result
    result: str | None = None
    error: str | None = None

    # Timing
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: float | None = None

    # Steps (for frontend timeline)
    steps: list[SubAgentStep] = field(default_factory=list)


# Default configs per agent type
SUBAGENT_DEFAULTS: dict[SubAgentType, dict[str, Any]] = {
    SubAgentType.EXPLORE: {
        "max_turns": 8,
        "max_tool_calls": 15,
        "max_tokens": 2048,
        "timeout_seconds": 60,
        "allowed_tool_categories": {"retrieval", "utility"},
        "system_prompt_suffix": (
            "You are an Explore agent. Quickly find information by searching "
            "the knowledge base and reading documents. You CANNOT modify data. "
            "Report findings concisely."
        ),
    },
    SubAgentType.TASK: {
        "max_turns": 15,
        "max_tool_calls": 30,
        "max_tokens": 2048,
        "timeout_seconds": 180,
        "allowed_tool_categories": None,  # All tools
        "system_prompt_suffix": (
            "You are a Task agent. Complete the assigned task autonomously "
            "using all available tools. Return your final result clearly."
        ),
    },
    SubAgentType.PLAN: {
        "max_turns": 5,
        "max_tool_calls": 10,
        "max_tokens": 2048,
        "timeout_seconds": 60,
        "allowed_tool_categories": {"retrieval", "utility"},
        "system_prompt_suffix": (
            "You are a Plan agent. Analyze the request and design a clear "
            "step-by-step implementation plan. Return structured instructions."
        ),
    },
}
