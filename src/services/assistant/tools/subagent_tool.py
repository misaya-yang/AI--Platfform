"""
spawn_subagent tool — registered in ToolRegistry so the LLM can
autonomously decide when to delegate work to a sub-agent.

ADR-003: The tool returns a special `__subagent__` marker in its result.
The agent_loop detects this and transparently runs the SubAgentManager,
forwarding SSE events and returning the summarized result to the main LLM.
"""

from __future__ import annotations

from ..agent.subagent_types import SubAgentConfig, SubAgentType
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

SPAWN_SUBAGENT_DEFINITION = ToolDefinition(
    name="spawn_subagent",
    description=(
        "Spawn a specialized sub-agent to handle a complex sub-task independently. "
        "Types: 'explore' (fast search, read-only), 'task' (full execution), 'plan' (create implementation plan). "
        "Sub-agents have their own context and return a summary."
    ),
    parameters=[
        ToolParameter(
            name="agent_type",
            type="string",
            description="Type: 'explore' (fast search), 'task' (full execution), 'plan' (create plan)",
            required=True,
            enum=["explore", "task", "plan"],
        ),
        ToolParameter(
            name="prompt",
            type="string",
            description="Detailed instructions for what the sub-agent should do",
            required=True,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="Short 3-5 word description of the task (shown in UI)",
            required=True,
        ),
        ToolParameter(
            name="context",
            type="string",
            description="Optional context from current conversation to pass to the sub-agent",
            required=False,
        ),
    ],
    category=ToolCategory.UTILITY,
    risk_level=ToolRiskLevel.LOW,
    requires_confirmation=False,
    when_to_use=(
        "Use when a task requires deep exploration, independent execution, "
        "or when you want to parallelize work. For example: "
        "'explore' to search the knowledge base thoroughly, "
        "'plan' to design an approach before executing, "
        "'task' to independently complete a complex sub-task."
    ),
    when_not_to_use="Don't use for simple direct questions or single tool calls.",
    examples=[
        ToolExample(
            description="Search for information",
            input={"agent_type": "explore", "prompt": "Search the knowledge base for all information about Zakat calculation methods", "description": "Search Zakat info"},
        ),
        ToolExample(
            description="Create a plan",
            input={"agent_type": "plan", "prompt": "Analyze the fasting rules across all four madhabs and create a comparison plan", "description": "Plan madhab comparison"},
        ),
    ],
    timeout_seconds=180,
    max_retries=0,
    is_async=True,
)


class SpawnSubAgentExecutor(ToolExecutor):
    """
    Executor that returns a __subagent__ marker.
    The actual execution happens in agent_loop.py which detects this marker.
    """

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        args = request.arguments
        try:
            config = SubAgentConfig(
                agent_type=SubAgentType(args["agent_type"]),
                prompt=args["prompt"],
                description=args.get("description", ""),
                parent_context=args.get("context"),
            )
        except (KeyError, ValueError) as e:
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
            result={"__subagent__": True, "config": config},
            metadata={"is_subagent": True},
        )


def register_subagent_tool() -> None:
    """Register the spawn_subagent tool in the global ToolRegistry."""
    register_tool(SPAWN_SUBAGENT_DEFINITION, SpawnSubAgentExecutor())
