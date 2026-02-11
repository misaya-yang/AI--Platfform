"""
Task Planning Prompts for Enterprise AI Assistant.

These prompts guide the Agent in:
1. Understanding user intent and requirements
2. Planning task execution strategies
3. Decomposing complex tasks into manageable steps
4. Selecting appropriate tools and execution approaches
5. Tracking progress and managing task state

Design Philosophy:
- Guardrails constrain tool usage and execution order
- Agent decides HOW to accomplish goals within constraints
- Explicit planning improves multi-step task completion
- Clear intent classification enables appropriate workflow selection
- Progress tracking enables adaptive execution

Prompt Types:
- TASK_PLANNING_SYSTEM_PROMPT: Core planning system prompt
- INTENT_ANALYSIS_PROMPT: User intent classification
- AGENTIC_TASK_DECOMPOSITION_PROMPT: Complex task breakdown
- TODO_TRACKING_PROMPT: Progress tracking and management
- TASK_REFINEMENT_PROMPT: Iterative plan improvement
- EXECUTION_REFLECTION_PROMPT: Post-execution learning

References:
- https://cookbook.openai.com/examples/gpt4-1_prompting_guide
- https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-4-best-practices
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
"""

from typing import Any

# =============================================================================
# Intent Types Definition
# =============================================================================

# Intent types for task classification
# Maps to SCENARIO_TYPES where applicable, with additional task-oriented types
INTENT_TYPES = {
    # Task-oriented intents (primary for planning)
    "document_creation": {
        "name": "Document Creation",
        "description": "Creating documents (PPT, Word, Excel, Markdown)",
        "doc_types": ["ppt", "pptx", "doc", "docx", "xls", "xlsx", "markdown"],
        "requires_tools": True,
        "typical_complexity": "complex",
    },
    "information_retrieval": {
        "name": "Information Retrieval",
        "description": "Searching and retrieving information from knowledge bases",
        "maps_to_scenario": "general_inquiry",
        "requires_tools": True,
        "typical_complexity": "moderate",
    },
    "data_analysis": {
        "name": "Data Analysis",
        "description": "Analyzing data, trends, metrics, and generating insights",
        "maps_to_scenario": "data_analysis",
        "requires_tools": True,
        "typical_complexity": "complex",
    },
    "comparison": {
        "name": "Comparison Analysis",
        "description": "Comparing products, options, or approaches",
        "maps_to_scenario": "product_inquiry",
        "requires_tools": True,
        "typical_complexity": "moderate",
    },
    "technical_task": {
        "name": "Technical Task",
        "description": "Code execution, configuration, or technical operations",
        "maps_to_scenario": "technical_support",
        "requires_tools": True,
        "typical_complexity": "complex",
    },
    "conversational": {
        "name": "Conversational",
        "description": "General conversation, clarification, or simple Q&A",
        "maps_to_scenario": "general_inquiry",
        "requires_tools": False,
        "typical_complexity": "simple",
    },
}

# Complexity levels for task assessment
COMPLEXITY_LEVELS = {
    "simple": {
        "description": "Single-step, direct answer possible",
        "strategy": "direct",
        "max_steps": 1,
    },
    "moderate": {
        "description": "Requires some research or multi-step response",
        "strategy": "sequential",
        "max_steps": 5,
    },
    "complex": {
        "description": "Requires multiple tools, analysis, or iterative work",
        "strategy": "parallel",
        "max_steps": 15,
    },
}


# =============================================================================
# Task Planning System Prompt
# =============================================================================

TASK_PLANNING_SYSTEM_PROMPT = """You are an intelligent task planning assistant.

<role>
## Your Responsibility

Analyze user requests and create optimal execution plans. Your plans should be comprehensive, efficient, and account for dependencies between tasks.
</role>

<guardrails>
## Execution Constraints (Non-Negotiable)

1. **Tool Usage Constraints**:
{tool_constraints}

2. **Execution Order Constraints**:
   - Information retrieval (search/query) MUST precede analysis
   - Content creation MUST precede document generation
   - Dependent tasks MUST wait for their dependencies to complete
   - Verification steps should follow implementation steps

3. **Safety Constraints**:
   - Never execute operations that could cause data loss without explicit backup steps
   - Include rollback procedures for reversible operations
   - Flag potentially destructive actions for user confirmation
</guardrails>

<agent_freedom>
## Your Decision Space

Within the constraints above, you decide:

- **Granularity**: How finely to decompose tasks (detailed vs coarse)
- **Parallelization**: Which tasks can execute concurrently
- **Tool Selection**: Which tools best accomplish each sub-task
- **Verification**: Whether and how to verify intermediate results
- **Error Recovery**: How to handle potential failures
- **Optimization**: When to batch or parallelize for efficiency
</agent_freedom>

<output_format>
## Output Format

Respond with valid JSON:
```json
{{
    "goal": "High-level task objective",
    "strategy": "Brief explanation of execution approach",
    "tasks": [
        {{
            "id": "task_1",
            "type": "retrieve|generate|analyze|transform|verify",
            "tool": "Tool name",
            "description": "What this task accomplishes",
            "parameters": {{}},
            "dependencies": [],
            "parallel_group": 1
        }}
    ],
    "notes": "Any important considerations or caveats"
}}
```

**Field Descriptions**:
- `type`: Category of operation (retrieve, generate, analyze, transform, verify)
- `dependencies`: Array of task IDs that must complete before this task
- `parallel_group`: Tasks with the same group number can execute in parallel
</output_format>"""


# =============================================================================
# Intent Analysis Prompt
# =============================================================================

INTENT_ANALYSIS_PROMPT = """Analyze the user request to determine intent and optimal handling strategy.

<user_request>
{request}
</user_request>

<intent_types>
{intent_types}
</intent_types>

<analysis_framework>
## Analysis Tasks

1. **Intent Classification**: Match to the most appropriate intent type
2. **Document Detection**: If document_creation, identify the document type
3. **Entity Extraction**: Extract key entities (topics, requirements, constraints)
4. **Complexity Assessment**: Evaluate based on scope and dependencies
5. **Strategy Selection**: Determine optimal execution approach

## Complexity Levels
- `simple`: Single-step, direct answer (no tools needed)
- `moderate`: Multi-step but straightforward (1-5 sequential steps)
- `complex`: Multiple tools, parallel execution, or iteration (5+ steps)

## Execution Strategies
- `direct`: Respond without tool use
- `sequential`: Execute steps one after another
- `parallel`: Execute independent steps concurrently
- `iterative`: Requires multiple rounds of refinement
</analysis_framework>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "intent_type": "document_creation|information_retrieval|data_analysis|comparison|technical_task|conversational",
    "document_type": "ppt|pptx|docx|xlsx|markdown|null",
    "entities": {{
        "topic": "Main topic or subject",
        "requirements": ["Specific requirement 1", "Specific requirement 2"],
        "constraints": ["Any constraints or limitations"]
    }},
    "complexity": "simple|moderate|complex",
    "strategy": "direct|sequential|parallel|iterative",
    "requires_tools": true|false,
    "suggested_tools": ["tool_name_1", "tool_name_2"],
    "confidence": 0.0-1.0
}}
</output_format>"""


# =============================================================================
# Agentic Task Decomposition Prompt
# =============================================================================

AGENTIC_TASK_DECOMPOSITION_PROMPT = """Decompose the following complex task into an actionable execution plan.

<task>
{task_description}
</task>

<context>
## Available Tools
{available_tools}

## Current State
{current_state}
</context>

<guardrails>
## Planning Constraints (Non-Negotiable)

1. **Dependency Ordering**: Tasks with dependencies MUST wait for prerequisites
2. **Tool Validation**: Only use tools that are available in the context
3. **Atomicity**: Each task must be independently executable and verifiable
4. **Safety First**: Include validation steps for operations that could fail
5. **State Tracking**: Every task must have clear success criteria
</guardrails>

<agent_freedom>
## Your Decision Space

- **Granularity**: How finely to decompose (more steps vs fewer)
- **Parallelization**: Which tasks can execute concurrently
- **Phase Organization**: How to group related tasks into phases
- **Error Handling**: Recovery strategies for potential failures
- **Optimization**: When to batch operations for efficiency
</agent_freedom>

<planning_workflow>
## Decomposition Process

1. **Analyze**: Parse the task; identify scope, inputs, and expected outputs
2. **Identify Dependencies**: Map which steps depend on which
3. **Phase Grouping**: Organize into logical execution phases
4. **Tool Mapping**: Assign appropriate tools to each step
5. **Risk Assessment**: Identify what could go wrong
</planning_workflow>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "analysis": "Your understanding of the task and key considerations",
    "phases": [
        {{
            "name": "Phase name",
            "objective": "What this phase accomplishes",
            "parallel_group": 1,
            "tasks": [
                {{
                    "id": "unique_id",
                    "action": "Specific action to take",
                    "tool": "Tool to use (null if no tool needed)",
                    "inputs": {{}},
                    "expected_output": "What success looks like",
                    "dependencies": [],
                    "fallback": "What to do if this fails (optional)"
                }}
            ]
        }}
    ],
    "success_criteria": ["Criterion 1", "Criterion 2"],
    "risks": [
        {{"risk": "Potential risk", "mitigation": "How to handle it"}}
    ],
    "estimated_steps": 5
}}
</output_format>"""


# =============================================================================
# TODO/Progress Tracking Prompt
# =============================================================================

TODO_TRACKING_PROMPT = """Track and manage task progress for the current workflow.

<original_goal>
{original_goal}
</original_goal>

<current_state>
## Completed Tasks
{completed_tasks}

## In Progress
{in_progress_tasks}

## Pending
{pending_tasks}

## Blocked Tasks
{blocked_tasks}
</current_state>

<analysis_requirements>
## Progress Analysis

1. **Completion Assessment**: What percentage of the goal is achieved?
2. **Blocker Identification**: What is preventing progress?
3. **Dependency Check**: Are pending tasks unblocked?
4. **Priority Evaluation**: What should be done next and why?
5. **Scope Validation**: Is the remaining work still aligned with the goal?
</analysis_requirements>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "progress_percentage": 0-100,
    "progress_summary": "Brief summary of what's been accomplished",
    "blockers": [
        {{"task_id": "id", "blocker": "What's blocking", "resolution": "Suggested fix"}}
    ],
    "next_action": {{
        "task_id": "ID of next task",
        "description": "What to do next",
        "reasoning": "Why this is the optimal next step",
        "dependencies_met": true|false
    }},
    "status_updates": [
        {{"task_id": "id", "new_status": "completed|in_progress|blocked|skipped", "reason": "Why this status"}}
    ],
    "remaining_work": {{
        "task_count": 5,
        "critical_path": ["task_id_1", "task_id_2"],
        "parallelizable": ["task_id_3", "task_id_4"]
    }},
    "goal_alignment": "on_track|at_risk|blocked|needs_replanning"
}}
</output_format>"""


# =============================================================================
# Task Refinement Prompt
# =============================================================================

TASK_REFINEMENT_PROMPT = """Refine the existing execution plan based on new information or execution results.

<original_plan>
{original_plan}
</original_plan>

<execution_results>
## Completed Steps
{completed_steps}

## Failed Steps
{failed_steps}

## New Information
{new_information}
</execution_results>

<refinement_goals>
## Refinement Objectives

1. **Incorporate Learning**: Apply insights from completed/failed steps
2. **Adjust Strategy**: Modify approach based on actual results
3. **Handle Failures**: Create recovery paths for failed steps
4. **Optimize Remaining**: Improve efficiency of pending tasks
5. **Validate Feasibility**: Ensure remaining plan is still achievable
</refinement_goals>

<guardrails>
## Refinement Constraints

- Preserve successfully completed work
- Do not invalidate dependencies that are already satisfied
- Failed steps must have new approaches, not just retries
- New steps must integrate with existing task IDs
</guardrails>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "refinement_summary": "What changed and why",
    "preserved_tasks": ["task_id_1", "task_id_2"],
    "modified_tasks": [
        {{
            "task_id": "id",
            "original_action": "What was planned",
            "new_action": "What to do instead",
            "reason": "Why this change"
        }}
    ],
    "new_tasks": [
        {{
            "id": "new_task_id",
            "action": "New action to take",
            "tool": "Tool to use",
            "dependencies": [],
            "insert_after": "task_id_to_follow"
        }}
    ],
    "removed_tasks": [
        {{"task_id": "id", "reason": "Why removed"}}
    ],
    "updated_success_criteria": ["Updated criterion 1", "Updated criterion 2"],
    "confidence": 0.0-1.0
}}
</output_format>"""


# =============================================================================
# Execution Reflection Prompt
# =============================================================================

EXECUTION_REFLECTION_PROMPT = """Reflect on the completed execution to extract learnings for future tasks.

<original_goal>
{original_goal}
</original_goal>

<execution_summary>
## Plan Overview
{plan_summary}

## Execution Timeline
{execution_timeline}

## Final Outcome
{final_outcome}
</execution_summary>

<reflection_framework>
## Reflection Areas

1. **Goal Achievement**: Was the original goal fully met?
2. **Planning Accuracy**: How well did the plan predict actual execution?
3. **Tool Effectiveness**: Which tools worked well/poorly?
4. **Time Efficiency**: Were there unexpected delays or optimizations?
5. **Error Patterns**: What failures occurred and why?
6. **Reusable Patterns**: What approaches should be repeated?
</reflection_framework>

<output_format>
Respond with valid JSON only (no markdown code blocks):
{{
    "goal_achievement": {{
        "status": "fully_met|partially_met|not_met",
        "explanation": "How well the goal was achieved",
        "gaps": ["Any unmet aspects"]
    }},
    "planning_accuracy": {{
        "score": 0.0-1.0,
        "overestimated": ["Things that were easier than expected"],
        "underestimated": ["Things that were harder than expected"],
        "unexpected": ["Things not anticipated in planning"]
    }},
    "tool_insights": [
        {{
            "tool": "tool_name",
            "effectiveness": "high|medium|low",
            "best_use_case": "When to use this tool",
            "limitations": ["Discovered limitations"]
        }}
    ],
    "efficiency_notes": {{
        "bottlenecks": ["What slowed execution"],
        "optimizations": ["What sped up execution"],
        "parallelization_opportunities": ["Steps that could have run in parallel"]
    }},
    "lessons_learned": [
        {{
            "lesson": "What was learned",
            "applies_to": "When this lesson applies",
            "recommendation": "How to apply in future"
        }}
    ],
    "reusable_patterns": [
        {{
            "pattern": "Pattern name",
            "description": "What it does",
            "trigger": "When to apply it"
        }}
    ]
}}
</output_format>"""


# =============================================================================
# Helper Functions
# =============================================================================


def get_intent_types_description() -> str:
    """Get formatted description of all intent types."""
    lines = []
    for code, info in INTENT_TYPES.items():
        lines.append(f"- **{code}** ({info['name']}): {info['description']}")
    return "\n".join(lines)


def get_intent_type_info(intent_type: str) -> dict[str, Any]:
    """Get information about a specific intent type."""
    return INTENT_TYPES.get(intent_type, INTENT_TYPES["conversational"])


def get_complexity_info(complexity: str) -> dict[str, Any]:
    """Get information about a complexity level."""
    return COMPLEXITY_LEVELS.get(complexity, COMPLEXITY_LEVELS["moderate"])


def _format_tools(tools: list[Any]) -> str:
    """Format tool list for prompts."""
    tool_descriptions = []
    for tool in tools:
        if isinstance(tool, str):
            tool_descriptions.append(f"- {tool}")
        elif isinstance(tool, dict):
            name = tool.get("function", {}).get("name", tool.get("name", "unknown"))
            desc = tool.get("function", {}).get("description", tool.get("description", ""))
            if desc:
                tool_descriptions.append(f"- **{name}**: {desc}")
            else:
                tool_descriptions.append(f"- {name}")
    return "\n".join(tool_descriptions) if tool_descriptions else "No specific tools available"


def _format_task_list(tasks: list[dict[str, Any]]) -> str:
    """Format task list for prompts."""
    if not tasks:
        return "None"
    lines = []
    for t in tasks:
        task_id = t.get("id", "unknown")
        desc = t.get("description", t.get("action", "No description"))
        status = t.get("status", "")
        if status:
            lines.append(f"- [{task_id}] {desc} (status: {status})")
        else:
            lines.append(f"- [{task_id}] {desc}")
    return "\n".join(lines)


# =============================================================================
# Prompt Builder Functions
# =============================================================================


def build_planning_prompt(
    user_request: str,
    available_tools: list[dict[str, Any]],
    tool_constraints: dict[str, dict[str, Any]] | None = None,
) -> str:
    """
    Build task planning prompt with guardrails.

    Args:
        user_request: User's request
        available_tools: List of available tools
        tool_constraints: Tool usage constraints (guardrails)

    Returns:
        Formatted planning prompt
    """
    tool_info = _format_tools(available_tools)

    # Format constraints
    if tool_constraints:
        constraint_lines = []
        for tool_name, constraints in tool_constraints.items():
            desc = constraints.get("description", "")
            constraint_lines.append(f"   - **{tool_name}**: {desc}")
        constraints_text = "\n".join(constraint_lines)
    else:
        constraints_text = "   No special constraints"

    system_prompt = TASK_PLANNING_SYSTEM_PROMPT.format(
        tool_constraints=constraints_text,
    )

    user_prompt = f"""Analyze the following user request and create an execution plan:

<user_request>
{user_request}
</user_request>

<available_tools>
{tool_info}
</available_tools>

Create an optimal execution plan."""

    return f"{system_prompt}\n\n---\n\n{user_prompt}"


def build_intent_analysis_prompt(request: str) -> str:
    """
    Build intent analysis prompt.

    Args:
        request: User's request

    Returns:
        Formatted intent analysis prompt
    """
    return INTENT_ANALYSIS_PROMPT.format(
        request=request,
        intent_types=get_intent_types_description(),
    )


def build_task_decomposition_prompt(
    task_description: str,
    available_tools: list[str],
    current_state: str = "Initial state - no prior work completed",
) -> str:
    """
    Build agentic task decomposition prompt.

    Args:
        task_description: Description of the complex task
        available_tools: List of available tool names
        current_state: Current execution state

    Returns:
        Formatted decomposition prompt
    """
    tools_str = _format_tools(available_tools)

    return AGENTIC_TASK_DECOMPOSITION_PROMPT.format(
        task_description=task_description,
        available_tools=tools_str,
        current_state=current_state,
    )


def build_todo_tracking_prompt(
    original_goal: str,
    completed_tasks: list[dict[str, Any]],
    in_progress_tasks: list[dict[str, Any]],
    pending_tasks: list[dict[str, Any]],
    blocked_tasks: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build TODO tracking prompt for progress management.

    Args:
        original_goal: The original goal being tracked
        completed_tasks: List of completed task details
        in_progress_tasks: List of in-progress task details
        pending_tasks: List of pending task details
        blocked_tasks: List of blocked task details (optional)

    Returns:
        Formatted tracking prompt
    """
    return TODO_TRACKING_PROMPT.format(
        original_goal=original_goal,
        completed_tasks=_format_task_list(completed_tasks),
        in_progress_tasks=_format_task_list(in_progress_tasks),
        pending_tasks=_format_task_list(pending_tasks),
        blocked_tasks=_format_task_list(blocked_tasks or []),
    )


def build_task_refinement_prompt(
    original_plan: str,
    completed_steps: list[dict[str, Any]],
    failed_steps: list[dict[str, Any]],
    new_information: str = "",
) -> str:
    """
    Build task refinement prompt for plan adjustment.

    Args:
        original_plan: The original execution plan (as JSON string or dict)
        completed_steps: List of completed step details
        failed_steps: List of failed step details with error info
        new_information: Any new information that affects the plan

    Returns:
        Formatted refinement prompt
    """
    # Format completed steps
    completed_str = _format_task_list(completed_steps) if completed_steps else "None"

    # Format failed steps with error details
    if failed_steps:
        failed_lines = []
        for step in failed_steps:
            step_id = step.get("id", "unknown")
            action = step.get("action", step.get("description", "No description"))
            error = step.get("error", "Unknown error")
            failed_lines.append(f"- [{step_id}] {action}\n  Error: {error}")
        failed_str = "\n".join(failed_lines)
    else:
        failed_str = "None"

    return TASK_REFINEMENT_PROMPT.format(
        original_plan=original_plan,
        completed_steps=completed_str,
        failed_steps=failed_str,
        new_information=new_information if new_information else "No new information",
    )


def build_execution_reflection_prompt(
    original_goal: str,
    plan_summary: str,
    execution_timeline: list[dict[str, Any]],
    final_outcome: str,
) -> str:
    """
    Build execution reflection prompt for post-task learning.

    Args:
        original_goal: The original goal that was pursued
        plan_summary: Summary of the execution plan
        execution_timeline: Timeline of execution events
        final_outcome: Description of the final outcome

    Returns:
        Formatted reflection prompt
    """
    # Format execution timeline
    if execution_timeline:
        timeline_lines = []
        for event in execution_timeline:
            timestamp = event.get("timestamp", "")
            action = event.get("action", event.get("description", ""))
            result = event.get("result", "")
            if timestamp:
                timeline_lines.append(f"- [{timestamp}] {action} → {result}")
            else:
                timeline_lines.append(f"- {action} → {result}")
        timeline_str = "\n".join(timeline_lines)
    else:
        timeline_str = "No timeline available"

    return EXECUTION_REFLECTION_PROMPT.format(
        original_goal=original_goal,
        plan_summary=plan_summary,
        execution_timeline=timeline_str,
        final_outcome=final_outcome,
    )
