"""
spawn_subagent tool — registered in ToolRegistry so the LLM can
autonomously decide when to delegate work to a sub-agent.

ADR-003: The tool returns a special single- or batch-subagent marker in its
result.  The streaming agent loop detects it and transparently runs the
SubAgentManager, forwarding events and returning terminal receipts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..agent.subagent_dispatch_runtime import canonical_sha256, stable_identifier
from ..agent.subagent_output_contract import normalize_output_schema
from ..agent.subagent_types import SUBAGENT_DEFAULTS, SubAgentConfig, SubAgentType
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

MAX_SUBAGENT_BATCH_SIZE = 5
DEFAULT_SUBAGENT_CONCURRENCY = 3

_BUILTIN_AGENT_IDS = [agent_type.value for agent_type in SubAgentType]


def _task_schema(agent_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "enum": _BUILTIN_AGENT_IDS,
                "description": "Built-in agent type.",
            },
            "agent_id": {
                "type": "string",
                "enum": agent_ids,
                "description": "Installed plugin specialist qualified id.",
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20_000,
                "description": "Detailed, self-contained instructions for this child.",
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Short task label shown in the UI.",
            },
            "context": {
                "type": "string",
                "maxLength": 20_000,
                "description": "Only the parent context required by this child.",
            },
            "task_id": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                "description": "Optional stable logical task id; the host derives one if omitted.",
            },
            "output_schema": {
                "type": "object",
                "description": "Optional bounded strict JSON Schema for this child's output.",
            },
        },
        "required": ["prompt", "description"],
        "oneOf": [
            {"required": ["agent_type"], "not": {"required": ["agent_id"]}},
            {"required": ["agent_id"], "not": {"required": ["agent_type"]}},
        ],
        "additionalProperties": False,
    }


def _argument_schema(agent_ids: list[str]) -> dict[str, Any]:
    single = _task_schema(agent_ids)
    task_properties = single["properties"]
    return {
        "type": "object",
        "properties": {
            **task_properties,
            "delegation_id": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                "description": (
                    "Optional stable logical delegation id; identical payloads receive a "
                    "deterministic host-derived id when omitted."
                ),
            },
            "description": {
                **task_properties["description"],
                "description": (
                    "Required short label for a single child; in tasks[] mode an optional "
                    "batch label that does not replace each child's description."
                ),
            },
            "context": {
                **task_properties["context"],
                "description": (
                    "Single-child context, or shared context inherited only by tasks[] items "
                    "that do not provide their own context."
                ),
            },
            "tasks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SUBAGENT_BATCH_SIZE,
                "items": single,
                "description": "Independent tasks to run concurrently; results retain input order.",
            },
            "max_concurrency": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SUBAGENT_BATCH_SIZE,
                "description": "Maximum simultaneous children (default 3).",
            },
        },
        "oneOf": [
            {
                "required": ["prompt", "description"],
                "oneOf": single["oneOf"],
                "not": {"required": ["tasks"]},
            },
            {
                "required": ["tasks"],
                "not": {
                    "anyOf": [
                        {"required": ["prompt"]},
                        {"required": ["agent_type"]},
                        {"required": ["agent_id"]},
                        {"required": ["task_id"]},
                        {"required": ["output_schema"]},
                    ]
                },
            },
        ],
        "additionalProperties": False,
    }


def _definition(agent_ids: list[str]) -> ToolDefinition:
    description = (
        "Delegate one complex sub-task or a bounded batch of independent sub-tasks. "
        "Use tasks[] for real parallel work (1-5 tasks, default concurrency 3); "
        "batch results are returned in input order. Built-ins: explore, task, plan. "
        "Installed specialists are selected through the host-validated agent_id enum."
    )
    return ToolDefinition(
        name="spawn_subagent",
        description=description,
        parameters=[
            ToolParameter(
                name="agent_type",
                type="string",
                description=(
                    "Type: 'explore' (fast search), 'task' (full execution), 'plan' (create plan)"
                ),
                required=False,
                enum=["explore", "task", "plan"],
            ),
            ToolParameter(
                name="prompt",
                type="string",
                description="Detailed instructions for what the sub-agent should do",
                required=False,
            ),
            ToolParameter(
                name="output_schema",
                type="object",
                description="Optional strict JSON Schema for a single child result",
                required=False,
            ),
            ToolParameter(
                name="delegation_id",
                type="string",
                description="Optional stable idempotency id for this delegation",
                required=False,
            ),
            ToolParameter(
                name="task_id",
                type="string",
                description="Optional stable logical id for a single child",
                required=False,
            ),
            ToolParameter(
                name="description",
                type="string",
                description="Short 3-5 word description of the task (shown in UI)",
                required=False,
            ),
            ToolParameter(
                name="context",
                type="string",
                description="Optional context from current conversation to pass to the sub-agent",
                required=False,
            ),
            ToolParameter(
                name="tasks",
                type="array",
                description="Independent child task objects to execute concurrently (maximum 5)",
                required=False,
                items=_task_schema(agent_ids),
            ),
            ToolParameter(
                name="max_concurrency",
                type="integer",
                description="Maximum concurrent children (1-5, default 3)",
                required=False,
            ),
        ],
        category=ToolCategory.UTILITY,
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        capability_metadata={
            # The host dispatch marker does not itself mutate external state.
            # Any child tool call is authorized and side-effect fenced again
            # inside SubAgentManager. Keeping max_retries=0 below prevents a
            # timed-out orchestration request from being replayed blindly.
            "operation_kind": "read",
            "read_only": True,
            "external_service": True,
        },
        when_to_use=(
            "Use when a task requires deep exploration or independent execution. "
            "Put mutually independent work in one tasks[] call to parallelize it. For example: "
            "'explore' to search the knowledge base thoroughly, "
            "'plan' to design an approach before executing, "
            "'task' to independently complete a complex sub-task."
        ),
        when_not_to_use="Don't use for simple direct questions or single tool calls.",
        examples=[
            ToolExample(
                description="Search for information",
                input={
                    "agent_type": "explore",
                    "prompt": (
                        "Search the knowledge base for all information about "
                        "Zakat calculation methods"
                    ),
                    "description": "Search Zakat info",
                },
            ),
            ToolExample(
                description="Create a plan",
                input={
                    "agent_type": "plan",
                    "prompt": (
                        "Analyze the fasting rules across all four madhabs and "
                        "create a comparison plan"
                    ),
                    "description": "Plan madhab comparison",
                },
            ),
        ],
        timeout_seconds=300,
        max_retries=0,
        is_async=True,
        argument_schema=_argument_schema(agent_ids),
    )


SPAWN_SUBAGENT_DEFINITION = _definition([])


def _profile_value(profile: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(profile, dict) and name in profile:
            return profile[name]
        if hasattr(profile, name):
            return getattr(profile, name)
    return default


def _positive_int(value: Any, fallback: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback
    )


def _profile_config(profile: Any, args: dict[str, Any]) -> SubAgentConfig:
    raw_type = str(_profile_value(profile, "base_type", default="explore") or "explore")
    try:
        agent_type = SubAgentType(raw_type)
    except ValueError as exc:
        raise ValueError(f"Unsupported profile base_type: {raw_type}") from exc
    defaults = SUBAGENT_DEFAULTS[agent_type]
    limits = _profile_value(profile, "limits", default=None)

    def limit(name: str) -> int:
        aliases = {
            "max_turns": ("initial_max_turns", "max_turns"),
            "max_tool_calls": ("initial_max_tool_calls", "max_tool_calls"),
            "max_tokens": ("recommended_max_tokens", "max_tokens"),
            "timeout_seconds": ("initial_timeout_seconds", "timeout_seconds"),
            "idle_timeout_seconds": ("idle_timeout_seconds",),
        }[name]
        direct = _profile_value(profile, *aliases, default=None)
        nested = _profile_value(limits, *aliases, default=None) if limits is not None else None
        return _positive_int(direct if direct is not None else nested, int(defaults[name]))

    qualified_id = str(_profile_value(profile, "qualified_id", "id", default="") or "").strip()
    if not qualified_id:
        raise ValueError("Profile is missing a qualified id")
    allowed_tools = _profile_value(profile, "allowed_tools", default=())
    allowed_categories = _profile_value(profile, "allowed_tool_categories", default=())
    return SubAgentConfig(
        agent_type=agent_type,
        prompt=str(args["prompt"]),
        description=str(args.get("description") or ""),
        parent_context=args.get("context"),
        max_turns=limit("max_turns"),
        max_tool_calls=limit("max_tool_calls"),
        max_tokens=limit("max_tokens"),
        timeout_seconds=limit("timeout_seconds"),
        idle_timeout_seconds=limit("idle_timeout_seconds"),
        adaptive_budget=True,
        output_schema=normalize_output_schema(args.get("output_schema")),
        profile_id=qualified_id,
        profile_name=str(_profile_value(profile, "name", default=qualified_id) or qualified_id),
        profile_instructions=str(_profile_value(profile, "instructions", default="") or ""),
        # Empty declarations deliberately mean deny all, never "inherit all".
        allowed_tools=frozenset(str(value) for value in (allowed_tools or ())),
        allowed_tool_categories=frozenset(str(value) for value in (allowed_categories or ())),
        definition_sha256=str(
            _profile_value(profile, "sha256", "content_sha256", "definition_sha256", default="")
            or ""
        ),
        source_plugin=str(
            _profile_value(profile, "plugin", "source_plugin", "source_path", default="") or ""
        ),
        task_id=str(args.get("task_id") or ""),
    )


class SpawnSubAgentExecutor(ToolExecutor):
    """
    Executor that returns a __subagent__ marker.
    The actual execution happens in agent_loop.py which detects this marker.
    """

    def __init__(self, agent_definitions: Iterable[Any] = ()) -> None:
        profiles: dict[str, Any] = {}
        for profile in agent_definitions:
            profile_id = str(
                _profile_value(profile, "qualified_id", "id", default="") or ""
            ).strip()
            if not profile_id or profile_id in _BUILTIN_AGENT_IDS or profile_id in profiles:
                raise ValueError(f"Invalid or duplicate sub-agent profile id: {profile_id!r}")
            profiles[profile_id] = profile
        self.agent_definitions = profiles

    def _config(self, args: dict[str, Any]) -> SubAgentConfig:
        profile_id = args.get("agent_id")
        agent_type = args.get("agent_type")
        if (profile_id is None) == (agent_type is None):
            raise ValueError("provide exactly one of agent_type or agent_id")
        if not isinstance(args.get("prompt"), str) or not args["prompt"].strip():
            raise ValueError("prompt must be a non-empty string")
        if not isinstance(args.get("description"), str) or not args["description"].strip():
            raise ValueError("description must be a non-empty string")
        if profile_id is not None:
            profile = self.agent_definitions.get(str(profile_id))
            if profile is None:
                raise ValueError(f"Unknown agent_id: {profile_id}")
            return _profile_config(profile, args)
        return SubAgentConfig(
            agent_type=SubAgentType(agent_type),
            prompt=str(args["prompt"]),
            description=str(args.get("description") or ""),
            parent_context=args.get("context"),
            output_schema=normalize_output_schema(args.get("output_schema")),
            task_id=str(args.get("task_id") or ""),
        )

    @staticmethod
    def _identity_payload(config: SubAgentConfig) -> dict[str, Any]:
        value = config.to_marker()
        for name in (
            "delegation_id",
            "task_id",
            "parent_task_id",
            "lineage",
            "depth",
            "dispatch_index",
        ):
            value.pop(name, None)
        return value

    def _assign_identities(
        self,
        configs: list[SubAgentConfig],
        *,
        explicit_delegation_id: Any,
        max_concurrency: int,
    ) -> tuple[str, str, list[SubAgentConfig]]:
        semantic_tasks = [self._identity_payload(config) for config in configs]
        delegation_id = stable_identifier(
            explicit_delegation_id,
            prefix="delegation",
            payload={"tasks": semantic_tasks, "max_concurrency": max_concurrency},
        )
        identified: list[SubAgentConfig] = []
        task_ids: set[str] = set()
        for index, config in enumerate(configs):
            task_id = stable_identifier(
                config.task_id or None,
                prefix="task",
                payload={
                    "delegation_id": delegation_id,
                    "dispatch_index": index,
                    "task": semantic_tasks[index],
                },
            )
            if task_id in task_ids:
                raise ValueError("task_id values must be unique within a delegation")
            task_ids.add(task_id)
            identified.append(
                replace(
                    config,
                    delegation_id=delegation_id,
                    task_id=task_id,
                    dispatch_index=index,
                )
            )
        request_sha256 = canonical_sha256(
            {
                "delegation_id": delegation_id,
                "max_concurrency": max_concurrency,
                "tasks": [config.to_marker() for config in identified],
            }
        )
        return delegation_id, request_sha256, identified

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        args = request.arguments
        try:
            raw_tasks = args.get("tasks")
            if raw_tasks is None:
                config = self._config(args)
                delegation_id, request_sha256, configs = self._assign_identities(
                    [config],
                    explicit_delegation_id=args.get("delegation_id"),
                    max_concurrency=1,
                )
                result: dict[str, Any] = {
                    "__subagent__": True,
                    "delegation_id": delegation_id,
                    "request_sha256": request_sha256,
                    "config": configs[0].to_marker(),
                }
                metadata = {"is_subagent": True, "batch_size": 1}
            else:
                # Enforce this independently from JSON Schema because the executor
                # is also callable directly by tests and internal runtimes.
                if "output_schema" in args:
                    raise ValueError("output_schema must be specified on each task")
                if not isinstance(raw_tasks, list) or not raw_tasks:
                    raise ValueError("tasks must be a non-empty array")
                if len(raw_tasks) > MAX_SUBAGENT_BATCH_SIZE:
                    raise ValueError(f"tasks exceeds maximum of {MAX_SUBAGENT_BATCH_SIZE}")
                shared_context = args.get("context")
                if shared_context is not None and not isinstance(shared_context, str):
                    raise ValueError("context must be a string")
                normalized_tasks = [
                    (
                        {**item, "context": shared_context}
                        if isinstance(item, dict)
                        and shared_context is not None
                        and "context" not in item
                        else item
                    )
                    for item in raw_tasks
                ]
                configs = [
                    replace(self._config(item), dispatch_index=index)
                    for index, item in enumerate(normalized_tasks)
                    if isinstance(item, dict)
                ]
                if len(configs) != len(raw_tasks):
                    raise ValueError("each task must be an object")
                concurrency = args.get("max_concurrency", DEFAULT_SUBAGENT_CONCURRENCY)
                if (
                    isinstance(concurrency, bool)
                    or not isinstance(concurrency, int)
                    or not 1 <= concurrency <= MAX_SUBAGENT_BATCH_SIZE
                ):
                    raise ValueError("max_concurrency must be an integer from 1 to 5")
                effective_concurrency = min(concurrency, len(configs))
                delegation_id, request_sha256, configs = self._assign_identities(
                    configs,
                    explicit_delegation_id=args.get("delegation_id"),
                    max_concurrency=effective_concurrency,
                )
                result = {
                    "__subagent_batch__": True,
                    "delegation_id": delegation_id,
                    "request_sha256": request_sha256,
                    "configs": [config.to_marker() for config in configs],
                    "max_concurrency": effective_concurrency,
                }
                metadata = {"is_subagent": True, "batch_size": len(configs)}
        except (KeyError, TypeError, ValueError) as e:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name="spawn_subagent",
                success=False,
                error=f"Invalid arguments: {e}",
            )

        return ToolCallResult(
            call_id=request.call_id,
            tool_name="spawn_subagent",
            success=True,
            result=result,
            metadata=metadata,
        )


def register_subagent_tool(agent_definitions: Iterable[Any] = ()) -> None:
    """Register the spawn_subagent tool in the global ToolRegistry."""
    profiles = tuple(agent_definitions)
    executor = SpawnSubAgentExecutor(profiles)
    definition = _definition(sorted(executor.agent_definitions))
    register_tool(definition, executor)
