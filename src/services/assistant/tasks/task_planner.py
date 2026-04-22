"""
Task Planner - Request Decomposition and Dependency Analysis.

This module provides intelligent task planning for complex user requests.
It analyzes user intent, decomposes requests into discrete tasks, and
builds dependency graphs to enable parallel tool execution.

Key Features:
- Task decomposition from natural language requests
- Dependency graph construction with topological sorting
- Parallel execution group identification
- Common workflow pattern recognition (comparison, report, search+answer)
- Circular dependency detection

Usage:
    ```python
    planner = TaskPlanner(model_client=anthropic_client)

    # Create execution plan from user request
    plan = await planner.create_plan(
        user_request="Compare product A and B, then generate a report",
        available_tools=["kb_search", "generate_document"]
    )

    # Execute tasks in parallel groups
    for group in plan.parallel_groups:
        tasks = [plan.get_task(tid) for tid in group]
        await asyncio.gather(*[execute(t) for t in tasks])
    ```

References:
- DAG-based task scheduling: https://en.wikipedia.org/wiki/Directed_acyclic_graph
- Topological sorting: https://en.wikipedia.org/wiki/Topological_sorting
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)


# =============================================================================
# Enums
# =============================================================================


class TaskType(str, Enum):
    """
    Classification of task types for execution planning.

    Each type corresponds to a category of tools and operations:
    - RETRIEVE: Information gathering from knowledge bases, web, or databases
    - GENERATE: Content creation including text, images, and documents
    - ANALYZE: Data processing, comparison, and insight extraction
    - TRANSFORM: Format conversion, translation, and data manipulation
    """

    RETRIEVE = "retrieve"  # KB search, web search, database queries
    GENERATE = "generate"  # Text generation, image creation, document synthesis
    ANALYZE = "analyze"  # Data analysis, comparison, summarization
    TRANSFORM = "transform"  # Format conversion, translation, restructuring


class IntentType(str, Enum):
    """
    Classification of user intent for intelligent task planning.

    Used by Agent to understand what the user really wants:
    - DOCUMENT_CREATION: Create documents (PPT, Word, Excel)
    - INFORMATION_QUERY: Answer questions using knowledge base
    - DATA_ANALYSIS: Analyze data and generate insights
    - COMPARISON: Compare multiple items
    - CREATIVE_WRITING: Creative content generation
    - CODE_EXECUTION: Run code or generate code
    """

    DOCUMENT_CREATION = "document_creation"
    INFORMATION_QUERY = "information_query"
    DATA_ANALYSIS = "data_analysis"
    COMPARISON = "comparison"
    CREATIVE_WRITING = "creative_writing"
    CODE_EXECUTION = "code_execution"
    GENERAL = "general"


class TaskStrategy(str, Enum):
    """
    Strategy for task execution.

    Agent decides which strategy to use:
    - SIMPLE: Single-step execution
    - SEQUENTIAL: Multi-step sequential execution
    - PARALLEL: Execute independent tasks in parallel
    - ITERATIVE: Iterative refinement with validation
    """

    SIMPLE = "simple"
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    ITERATIVE = "iterative"


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class PlannedTask:
    """
    A single task within an execution plan.

    Represents an atomic unit of work that can be executed by a tool.
    Tasks can have dependencies on other tasks, forming a directed
    acyclic graph (DAG) for execution scheduling.

    Attributes:
        id: Unique identifier for this task within the plan
        type: Category of the task (retrieve, generate, analyze, transform)
        tool: Name of the tool to execute this task
        description: Human-readable description of what this task does
        parameters: Arguments to pass to the tool
        dependencies: Set of task IDs that must complete before this task
        priority: Execution priority (higher = more important, default 0)
        estimated_duration_ms: Estimated execution time in milliseconds

    Example:
        ```python
        task = PlannedTask(
            id="search_product_a",
            type=TaskType.RETRIEVE,
            tool="kb_search",
            description="Search knowledge base for Product A specifications",
            parameters={"query": "Product A specifications", "top_k": 5},
            dependencies=set(),  # No dependencies, can run first
        )
        ```
    """

    id: str
    type: TaskType
    tool: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    dependencies: set[str] = field(default_factory=set)
    priority: int = 0
    estimated_duration_ms: int = 1000

    def __post_init__(self):
        """Ensure dependencies is a set."""
        if self.dependencies is None:
            self.dependencies = set()
        elif not isinstance(self.dependencies, set):
            self.dependencies = set(self.dependencies)

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize task to dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "id": self.id,
            "type": self.type.value,
            "tool": self.tool,
            "description": self.description,
            "parameters": self.parameters,
            "dependencies": list(self.dependencies),
            "priority": self.priority,
            "estimated_duration_ms": self.estimated_duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannedTask:
        """
        Deserialize task from dictionary.

        Args:
            data: Dictionary containing task data

        Returns:
            PlannedTask instance
        """
        return cls(
            id=data["id"],
            type=TaskType(data["type"]),
            tool=data["tool"],
            description=data["description"],
            parameters=data.get("parameters", {}),
            dependencies=set(data.get("dependencies", [])),
            priority=data.get("priority", 0),
            estimated_duration_ms=data.get("estimated_duration_ms", 1000),
        )


@dataclass
class ExecutionPlan:
    """
    Complete execution plan with task dependency graph.

    Contains all tasks needed to fulfill a user request, organized
    into parallel execution groups based on dependency analysis.

    Attributes:
        goal: High-level description of what this plan achieves
        tasks: List of all tasks in the plan
        parallel_groups: List of task ID groups that can execute in parallel
                        Each group must complete before the next group starts
        metadata: Additional plan metadata (e.g., estimated total time, confidence)

    Example:
        ```python
        plan = ExecutionPlan(
            goal="Compare Product A and Product B specifications",
            tasks=[task_search_a, task_search_b, task_compare],
            parallel_groups=[
                ["search_a", "search_b"],  # Can run in parallel
                ["compare"],               # Must wait for searches
            ],
        )

        # Execute plan
        for group in plan.parallel_groups:
            tasks = [plan.get_task(tid) for tid in group]
            results = await asyncio.gather(*[execute(t) for t in tasks])
        ```
    """

    goal: str
    tasks: list[PlannedTask] = field(default_factory=list)
    parallel_groups: list[list[str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def get_task(self, task_id: str) -> PlannedTask | None:
        """
        Get a task by its ID.

        Args:
            task_id: The unique identifier of the task

        Returns:
            The PlannedTask if found, None otherwise
        """
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_tasks_by_type(self, task_type: TaskType) -> list[PlannedTask]:
        """
        Get all tasks of a specific type.

        Args:
            task_type: The type of tasks to retrieve

        Returns:
            List of tasks matching the specified type
        """
        return [t for t in self.tasks if t.type == task_type]

    def get_root_tasks(self) -> list[PlannedTask]:
        """
        Get tasks with no dependencies (can start immediately).

        Returns:
            List of tasks that have no dependencies
        """
        return [t for t in self.tasks if not t.dependencies]

    def get_leaf_tasks(self) -> list[PlannedTask]:
        """
        Get tasks that no other task depends on (final outputs).

        Returns:
            List of tasks that are not dependencies of any other task
        """
        all_deps = set()
        for task in self.tasks:
            all_deps.update(task.dependencies)

        return [t for t in self.tasks if t.id not in all_deps]

    def get_total_estimated_duration(self) -> int:
        """
        Estimate total execution time based on parallel groups.

        Returns:
            Estimated total duration in milliseconds
        """
        total = 0
        for group in self.parallel_groups:
            # For parallel execution, take the max duration in each group
            group_max = 0
            for task_id in group:
                task = self.get_task(task_id)
                if task:
                    group_max = max(group_max, task.estimated_duration_ms)
            total += group_max
        return total

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize plan to dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "goal": self.goal,
            "tasks": [t.to_dict() for t in self.tasks],
            "parallel_groups": self.parallel_groups,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionPlan:
        """
        Deserialize plan from dictionary.

        Args:
            data: Dictionary containing plan data

        Returns:
            ExecutionPlan instance
        """
        return cls(
            goal=data["goal"],
            tasks=[PlannedTask.from_dict(t) for t in data.get("tasks", [])],
            parallel_groups=data.get("parallel_groups", []),
            metadata=data.get("metadata", {}),
        )


class CircularDependencyError(Exception):
    """
    Raised when a circular dependency is detected in the task graph.

    Attributes:
        cycle: List of task IDs forming the cycle
    """

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        cycle_str = " -> ".join(cycle)
        super().__init__(f"Circular dependency detected: {cycle_str}")


# =============================================================================
# Workflow Pattern Templates
# =============================================================================


@dataclass
class WorkflowPattern:
    """
    Template for common workflow patterns.

    Defines a reusable pattern of tasks and their dependencies
    for frequently occurring request types.

    Attributes:
        name: Identifier for the pattern
        description: What this pattern is used for
        task_templates: List of task template dictionaries
        keywords: Keywords that suggest this pattern applies
    """

    name: str
    description: str
    task_templates: list[dict[str, Any]]
    keywords: list[str] = field(default_factory=list)


# =============================================================================
# Task Planner
# =============================================================================


class TaskPlanner:
    """
    Analyzes user requests and generates execution plans with dependency graphs.

    The TaskPlanner is responsible for:
    1. Understanding user intent from natural language requests
    2. Decomposing complex requests into discrete, executable tasks
    3. Identifying dependencies between tasks
    4. Grouping independent tasks for parallel execution
    5. Detecting and preventing circular dependencies

    Workflow Patterns:
        The planner recognizes common patterns and can apply templates:
        - comparison: Gather data about multiple items, then compare
        - report: Collect information, analyze, and generate document
        - search_and_answer: Search knowledge base, then synthesize answer
        - multi_search: Execute multiple parallel searches

    Usage:
        ```python
        # With LLM for intent understanding
        planner = TaskPlanner(model_client=anthropic_client)
        plan = await planner.create_plan(
            user_request="Compare Tesla and BMW electric vehicles",
            available_tools=["kb_search", "web_search", "generate_comparison"]
        )

        # Without LLM (rule-based only)
        planner = TaskPlanner()
        plan = await planner.create_plan(
            user_request="Search for product specifications",
            available_tools=["kb_search"]
        )
        ```

    Thread Safety:
        The TaskPlanner is stateless and thread-safe. Each call to
        create_plan operates independently.
    """

    # Common workflow patterns as class attribute for pattern matching
    WORKFLOW_PATTERNS: dict[str, WorkflowPattern] = {
        "comparison": WorkflowPattern(
            name="comparison",
            description="Compare two or more items by gathering data and analyzing differences",
            task_templates=[
                {
                    "id_prefix": "retrieve",
                    "type": TaskType.RETRIEVE,
                    "tool": "kb_search",
                    "description_template": "Search for information about {item}",
                    "dependencies": [],
                    "repeat_for": "items",  # Repeat for each item being compared
                },
                {
                    "id": "analyze_comparison",
                    "type": TaskType.ANALYZE,
                    "tool": "analyze",
                    "description": "Compare and analyze the gathered information",
                    "depends_on_all": "retrieve",  # Depends on all retrieve tasks
                },
                {
                    "id": "generate_output",
                    "type": TaskType.GENERATE,
                    "tool": "generate_text",
                    "description": "Generate comparison summary or report",
                    "dependencies": ["analyze_comparison"],
                },
            ],
            keywords=[
                "compare",
                "comparison",
                "versus",
                "vs",
                "difference",
                "differences",
                "better",
                "which",
            ],
        ),
        "report": WorkflowPattern(
            name="report",
            description="Generate a comprehensive report by gathering and synthesizing information",
            task_templates=[
                {
                    "id": "retrieve_data",
                    "type": TaskType.RETRIEVE,
                    "tool": "kb_search",
                    "description": "Retrieve relevant data for the report",
                    "dependencies": [],
                },
                {
                    "id": "analyze_data",
                    "type": TaskType.ANALYZE,
                    "tool": "analyze",
                    "description": "Analyze and structure the retrieved data",
                    "dependencies": ["retrieve_data"],
                },
                {
                    "id": "generate_report",
                    "type": TaskType.GENERATE,
                    "tool": "generate_document",
                    "description": "Generate the final report document",
                    "dependencies": ["analyze_data"],
                },
            ],
            keywords=["report", "document", "summary", "brief", "overview", "write up"],
        ),
        "search_and_answer": WorkflowPattern(
            name="search_and_answer",
            description="Search for information and provide a synthesized answer",
            task_templates=[
                {
                    "id": "search",
                    "type": TaskType.RETRIEVE,
                    "tool": "kb_search",
                    "description": "Search knowledge base for relevant information",
                    "dependencies": [],
                },
                {
                    "id": "generate_answer",
                    "type": TaskType.GENERATE,
                    "tool": "generate_text",
                    "description": "Synthesize answer from search results",
                    "dependencies": ["search"],
                },
            ],
            keywords=["find", "search", "look up", "what is", "how to", "explain", "tell me"],
        ),
        "multi_search": WorkflowPattern(
            name="multi_search",
            description="Execute multiple parallel searches and combine results",
            task_templates=[
                {
                    "id_prefix": "search",
                    "type": TaskType.RETRIEVE,
                    "tool": "kb_search",
                    "description_template": "Search for {query}",
                    "dependencies": [],
                    "repeat_for": "queries",
                },
                {
                    "id": "combine_results",
                    "type": TaskType.ANALYZE,
                    "tool": "analyze",
                    "description": "Combine and deduplicate search results",
                    "depends_on_all": "search",
                },
            ],
            keywords=["multiple", "several", "various", "different sources", "both", "all"],
        ),
        "translate": WorkflowPattern(
            name="translate",
            description="Translate content from one language to another",
            task_templates=[
                {
                    "id": "retrieve_content",
                    "type": TaskType.RETRIEVE,
                    "tool": "kb_search",
                    "description": "Retrieve content to translate",
                    "dependencies": [],
                    "optional": True,  # May already have content
                },
                {
                    "id": "translate",
                    "type": TaskType.TRANSFORM,
                    "tool": "translate",
                    "description": "Translate the content",
                    "dependencies": [],  # Or ["retrieve_content"] if retrieval needed
                },
            ],
            keywords=[
                "translate",
                "translation",
                "convert to",
                "in spanish",
                "in chinese",
                "in french",
            ],
        ),
        "image_generation": WorkflowPattern(
            name="image_generation",
            description="Generate images based on description",
            task_templates=[
                {
                    "id": "generate_image",
                    "type": TaskType.GENERATE,
                    "tool": "generate_image",
                    "description": "Generate image based on prompt",
                    "dependencies": [],
                },
            ],
            keywords=["generate image", "create image", "draw", "picture of", "illustration"],
        ),
    }

    # Intent detection keywords for intelligent planning
    INTENT_KEYWORDS: dict[IntentType, list[str]] = {
        IntentType.DOCUMENT_CREATION: [
            "写",
            "生成",
            "制作",
            "创建",
            "做一个",
            "ppt",
            "word",
            "excel",
            "文档",
            "报告",
            "演示",
            "write",
            "create",
            "generate",
            "make",
            "produce",
        ],
        IntentType.INFORMATION_QUERY: [
            "什么是",
            "如何",
            "怎么",
            "为什么",
            "解释",
            "查找",
            "搜索",
            "告诉我",
            "what",
            "how",
            "why",
            "explain",
            "find",
            "search",
        ],
        IntentType.DATA_ANALYSIS: [
            "分析",
            "统计",
            "计算",
            "数据",
            "趋势",
            "analyze",
            "statistics",
            "calculate",
            "data",
            "trend",
        ],
        IntentType.COMPARISON: [
            "比较",
            "对比",
            "区别",
            "差异",
            "哪个更",
            "compare",
            "versus",
            "vs",
            "difference",
            "better",
        ],
        IntentType.CREATIVE_WRITING: [
            "写一篇",
            "创作",
            "编写",
            "撰写",
            "文章",
            "故事",
            "write",
            "compose",
            "article",
            "story",
            "essay",
        ],
        IntentType.CODE_EXECUTION: [
            "运行",
            "执行",
            "代码",
            "脚本",
            "python",
            "run",
            "execute",
            "code",
            "script",
            "program",
        ],
    }

    # Tool to TaskType mapping for automatic type inference
    TOOL_TYPE_MAPPING: dict[str, TaskType] = {
        "kb_search": TaskType.RETRIEVE,
        "web_search": TaskType.RETRIEVE,
        "tavily_search": TaskType.RETRIEVE,
        "database_query": TaskType.RETRIEVE,
        "generate_text": TaskType.GENERATE,
        "generate_document": TaskType.GENERATE,
        "generate_image": TaskType.GENERATE,
        "analyze": TaskType.ANALYZE,
        "compare": TaskType.ANALYZE,
        "summarize": TaskType.ANALYZE,
        "translate": TaskType.TRANSFORM,
        "convert_format": TaskType.TRANSFORM,
        "extract_data": TaskType.TRANSFORM,
    }

    def __init__(
        self,
        model_client: Any | None = None,
        model_name: str | None = None,
    ):
        """
        Initialize the TaskPlanner.

        Args:
            model_client: Optional LLM client for advanced intent understanding.
                         If not provided, uses rule-based planning only.
            model_name: Model identifier for LLM calls (e.g., "claude-3-sonnet-20240229")
        """
        self.model_client = model_client
        self.model_name = model_name or "claude-3-haiku-20240307"

    async def create_plan(
        self,
        user_request: str,
        available_tools: list[str] | list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        use_llm: bool = True,
    ) -> ExecutionPlan:
        """
        Create an execution plan from a user request.

        Analyzes the user's request and generates a plan containing:
        - Discrete tasks with tool assignments
        - Dependencies between tasks
        - Parallel execution groups

        Args:
            user_request: Natural language description of what the user wants
            available_tools: List of tool names or tool definitions
            context: Additional context (e.g., session info, user preferences)
            use_llm: Whether to use LLM for intent understanding (requires model_client)

        Returns:
            ExecutionPlan with tasks and parallel execution groups
        """
        available_tools = available_tools or []
        context = context or {}

        logger.info(f"Creating plan for request: {user_request[:100]}...")

        # Extract tool names for rule-based logic and validation
        available_tool_names = []
        for tool in available_tools:
            if isinstance(tool, str):
                available_tool_names.append(tool)
            elif isinstance(tool, dict):
                available_tool_names.append(
                    tool.get("function", {}).get("name", tool.get("name", "unknown"))
                )

        # Step 1: Analyze user intent (Agent intelligence)
        intent_type, strategy, intent_metadata = self.analyze_intent(user_request)

        # Step 2: Detect workflow pattern from keywords
        detected_pattern = self._detect_pattern(user_request)

        # Step 3: Generate tasks (via LLM or rule-based)
        if use_llm and self.model_client:
            tasks = await self._create_plan_with_llm(
                user_request, available_tools, context, detected_pattern
            )
        else:
            tasks = self._create_plan_rule_based(
                user_request, available_tool_names, context, detected_pattern
            )

        # Step 4: Analyze dependencies and create parallel groups
        parallel_groups = self.analyze_dependencies(tasks)

        # Step 5: Build execution plan with intent analysis
        plan = ExecutionPlan(
            goal=user_request,
            tasks=tasks,
            parallel_groups=parallel_groups,
            metadata={
                "detected_pattern": detected_pattern.name if detected_pattern else None,
                "tool_count": len({t.tool for t in tasks}),
                "task_count": len(tasks),
                "parallel_group_count": len(parallel_groups),
                # Agent intelligence: intent analysis results
                "intent_type": intent_type.value,
                "strategy": strategy.value,
                **intent_metadata,
            },
        )

        logger.info(
            f"Created plan with {len(tasks)} tasks in {len(parallel_groups)} groups "
            f"(pattern: {detected_pattern.name if detected_pattern else 'none'})"
        )

        return plan

    def _detect_pattern(self, user_request: str) -> WorkflowPattern | None:
        """
        Detect which workflow pattern matches the user request.

        Uses keyword matching to identify common patterns.

        Args:
            user_request: The user's request text

        Returns:
            Matching WorkflowPattern or None if no pattern matches
        """
        request_lower = user_request.lower()

        best_match = None
        best_score = 0

        for pattern in self.WORKFLOW_PATTERNS.values():
            score = sum(1 for kw in pattern.keywords if kw in request_lower)
            if score > best_score:
                best_score = score
                best_match = pattern

        if best_score > 0:
            logger.debug(f"Detected pattern: {best_match.name} (score: {best_score})")
            return best_match

        return None

    def analyze_intent(self, user_request: str) -> tuple[IntentType, TaskStrategy, dict[str, Any]]:
        """
        Analyze user intent to understand what they really want.

        Agent uses this to make intelligent decisions about:
        - What type of task this is
        - What strategy to use for execution
        - What constraints apply

        Args:
            user_request: The user's request text

        Returns:
            Tuple of (IntentType, TaskStrategy, metadata dict)
        """
        request_lower = user_request.lower()
        scores: dict[IntentType, int] = dict.fromkeys(IntentType, 0)

        # Score each intent type
        for intent_type, keywords in self.INTENT_KEYWORDS.items():
            for kw in keywords:
                if kw in request_lower:
                    scores[intent_type] += 1

        # Find best matching intent
        best_intent = max(scores, key=scores.get)
        if scores[best_intent] == 0:
            best_intent = IntentType.GENERAL

        # Determine strategy based on intent
        strategy = self._select_strategy(best_intent, user_request)

        # Extract metadata for the intent
        metadata = self._extract_intent_metadata(best_intent, user_request)

        logger.info(f"Analyzed intent: {best_intent.value}, strategy: {strategy.value}")

        return best_intent, strategy, metadata

    def _select_strategy(self, intent: IntentType, user_request: str) -> TaskStrategy:
        """
        Select execution strategy based on intent and request complexity.

        Agent decides which approach will work best.

        Args:
            intent: Detected user intent
            user_request: The user's request

        Returns:
            TaskStrategy to use
        """
        # Document creation usually requires iterative refinement with validation
        if intent == IntentType.DOCUMENT_CREATION:
            return TaskStrategy.ITERATIVE

        # Comparisons need parallel data gathering
        if intent == IntentType.COMPARISON:
            return TaskStrategy.PARALLEL

        # Data analysis is usually sequential
        if intent == IntentType.DATA_ANALYSIS:
            return TaskStrategy.SEQUENTIAL

        # Simple queries can be handled directly
        if intent == IntentType.INFORMATION_QUERY:
            return TaskStrategy.SIMPLE

        # Default to sequential for unknown intents
        return TaskStrategy.SEQUENTIAL

    def _extract_intent_metadata(
        self,
        intent: IntentType,
        user_request: str,
    ) -> dict[str, Any]:
        """
        Extract metadata relevant to the detected intent.

        Args:
            intent: Detected intent type
            user_request: The user's request

        Returns:
            Dict with intent-specific metadata
        """
        metadata: dict[str, Any] = {"intent": intent.value}
        request_lower = user_request.lower()

        if intent == IntentType.DOCUMENT_CREATION:
            # Detect document type
            if "ppt" in request_lower or "演示" in request_lower:
                metadata["document_type"] = "ppt"
            elif "word" in request_lower or "文档" in request_lower:
                metadata["document_type"] = "docx"
            elif "excel" in request_lower or "表格" in request_lower:
                metadata["document_type"] = "xlsx"
            else:
                metadata["document_type"] = "markdown"

        elif intent == IntentType.COMPARISON:
            # Extract items to compare
            items = self._extract_comparison_items(user_request)
            metadata["comparison_items"] = items

        return metadata

    def _create_plan_rule_based(
        self,
        user_request: str,
        available_tools: list[str],
        context: dict[str, Any],
        pattern: WorkflowPattern | None,
    ) -> list[PlannedTask]:
        """
        Create tasks using rule-based logic without LLM.

        Uses detected pattern and available tools to generate tasks.
        Falls back to simple single-task plan if no pattern matches.

        Args:
            user_request: The user's request
            available_tools: Tools available for use
            context: Additional context
            pattern: Detected workflow pattern (if any)

        Returns:
            List of PlannedTask objects
        """
        tasks: list[PlannedTask] = []

        if pattern:
            # Apply pattern template
            tasks = self._apply_pattern_template(pattern, user_request, available_tools, context)
        else:
            # Default: single retrieval task if no pattern matches
            if "kb_search" in available_tools:
                tasks.append(
                    PlannedTask(
                        id="search_1",
                        type=TaskType.RETRIEVE,
                        tool="kb_search",
                        description=f"Search for: {user_request}",
                        parameters={"query": user_request},
                    )
                )
            elif available_tools:
                # Use first available tool
                tool = available_tools[0]
                task_type = self.TOOL_TYPE_MAPPING.get(tool, TaskType.RETRIEVE)
                tasks.append(
                    PlannedTask(
                        id="task_1",
                        type=task_type,
                        tool=tool,
                        description=user_request,
                        parameters={"input": user_request},
                    )
                )

        return tasks

    def _apply_pattern_template(
        self,
        pattern: WorkflowPattern,
        user_request: str,
        available_tools: list[str],
        context: dict[str, Any],
    ) -> list[PlannedTask]:
        """
        Apply a workflow pattern template to generate tasks.

        Args:
            pattern: The workflow pattern to apply
            user_request: The user's request
            available_tools: Available tools
            context: Additional context

        Returns:
            List of PlannedTask objects from the pattern
        """
        tasks: list[PlannedTask] = []
        task_id_map: dict[str, list[str]] = defaultdict(list)  # prefix -> actual ids

        # Extract items to compare (for comparison pattern)
        items = self._extract_comparison_items(user_request) if pattern.name == "comparison" else []

        for template in pattern.task_templates:
            # Check if this is a repeated task template
            if template.get("repeat_for") == "items" and items:
                for i, item in enumerate(items):
                    task_id = f"{template.get('id_prefix', 'task')}_{i + 1}"
                    description = template.get("description_template", "").format(item=item)

                    # Check if tool is available
                    tool = template["tool"]
                    if tool not in available_tools and available_tools:
                        # Try to find alternative tool
                        tool = self._find_alternative_tool(tool, available_tools)

                    if tool:
                        task = PlannedTask(
                            id=task_id,
                            type=template["type"],
                            tool=tool,
                            description=description,
                            parameters={"query": item},
                            dependencies=set(template.get("dependencies", [])),
                        )
                        tasks.append(task)
                        task_id_map[template.get("id_prefix", "task")].append(task_id)

            elif template.get("repeat_for") == "queries":
                # For multi_search pattern - would need query extraction
                # Simplified: create a single search task
                task_id = template.get("id", f"task_{len(tasks) + 1}")
                tool = template["tool"]

                if tool not in available_tools and available_tools:
                    tool = self._find_alternative_tool(tool, available_tools)

                if tool:
                    task = PlannedTask(
                        id=task_id,
                        type=template["type"],
                        tool=tool,
                        description=template.get("description", user_request),
                        parameters={"query": user_request},
                        dependencies=set(template.get("dependencies", [])),
                    )
                    tasks.append(task)
                    prefix = template.get("id_prefix", "task")
                    task_id_map[prefix].append(task_id)

            else:
                # Regular single task
                task_id = template.get("id", f"task_{len(tasks) + 1}")
                tool = template["tool"]

                if tool not in available_tools and available_tools:
                    tool = self._find_alternative_tool(tool, available_tools)

                if tool or not available_tools:  # Allow if no tools specified
                    dependencies = set(template.get("dependencies", []))

                    # Handle "depends_on_all" pattern
                    if template.get("depends_on_all"):
                        prefix = template["depends_on_all"]
                        dependencies = set(task_id_map.get(prefix, []))

                    task = PlannedTask(
                        id=task_id,
                        type=template["type"],
                        tool=tool or template["tool"],
                        description=template.get("description", ""),
                        parameters={},
                        dependencies=dependencies,
                    )
                    tasks.append(task)

        return tasks

    def _extract_comparison_items(self, request: str) -> list[str]:
        """
        Extract items to compare from a comparison request.

        Args:
            request: User request string

        Returns:
            List of items to compare
        """
        request_lower = request.lower()

        # Pattern: "compare X and Y"
        match = re.search(
            r"compare\s+(.+?)\s+(?:and|vs|versus|with)\s+(.+?)(?:\s*[,.]|$)",
            request_lower,
            re.IGNORECASE,
        )
        if match:
            return [match.group(1).strip(), match.group(2).strip()]

        # Pattern: "X vs Y"
        match = re.search(
            r"(\w+(?:\s+\w+)*)\s+(?:vs|versus)\s+(\w+(?:\s+\w+)*)", request_lower, re.IGNORECASE
        )
        if match:
            return [match.group(1).strip(), match.group(2).strip()]

        # Pattern: "difference between X and Y"
        match = re.search(
            r"difference(?:s)?\s+between\s+(.+?)\s+and\s+(.+?)(?:\s*[,.]|$)",
            request_lower,
            re.IGNORECASE,
        )
        if match:
            return [match.group(1).strip(), match.group(2).strip()]

        return []

    def _find_alternative_tool(self, tool: str, available_tools: list[str]) -> str | None:
        """
        Find an alternative tool when the specified tool is not available.

        Args:
            tool: The requested tool name
            available_tools: List of available tool names

        Returns:
            Alternative tool name or None
        """
        # Get the type of the original tool
        original_type = self.TOOL_TYPE_MAPPING.get(tool)

        if original_type:
            # Find a tool of the same type
            for avail_tool in available_tools:
                if self.TOOL_TYPE_MAPPING.get(avail_tool) == original_type:
                    return avail_tool

        return None

    async def _create_plan_with_llm(
        self,
        user_request: str,
        available_tools: list[Any],
        context: dict[str, Any],
        pattern: WorkflowPattern | None,
    ) -> list[PlannedTask]:
        """
        Create tasks using LLM for intelligent decomposition.

        Uses the model_client to analyze the request and generate
        a structured task plan.

        Args:
            user_request: The user's request
            available_tools: Tools available for use
            context: Additional context
            pattern: Detected workflow pattern (if any)

        Returns:
            List of PlannedTask objects
        """
        # Build prompt for task decomposition
        prompt = self._build_decomposition_prompt(user_request, available_tools, pattern)

        try:
            # Call LLM for task decomposition
            response = await self._call_llm(prompt)

            # Extract tool names for validation
            available_tool_names = []
            for tool in available_tools:
                if isinstance(tool, str):
                    available_tool_names.append(tool)
                elif isinstance(tool, dict):
                    available_tool_names.append(
                        tool.get("function", {}).get("name", tool.get("name", "unknown"))
                    )

            tasks = self._parse_llm_response(response, available_tool_names)

            if tasks:
                return tasks
            else:
                # Fall back to rule-based if LLM response parsing fails
                logger.warning("LLM response parsing failed, falling back to rule-based")
                return self._create_plan_rule_based(
                    user_request, available_tool_names, context, pattern
                )

        except Exception as e:
            logger.error(f"LLM planning failed: {e}")
            # Fall back to rule-based planning
            # Extract tool names for rule-based logic
            available_tool_names = []
            for tool in available_tools:
                if isinstance(tool, str):
                    available_tool_names.append(tool)
                elif isinstance(tool, dict):
                    available_tool_names.append(
                        tool.get("function", {}).get("name", tool.get("name", "unknown"))
                    )

            return self._create_plan_rule_based(
                user_request, available_tool_names, context, pattern
            )

    def _build_decomposition_prompt(
        self,
        user_request: str,
        available_tools: list[Any],
        pattern: WorkflowPattern | None,
    ) -> str:
        """
        Build the prompt for LLM task decomposition.

        Args:
            user_request: The user's request
            available_tools: Available tools
            pattern: Detected pattern (for context)

        Returns:
            Prompt string for the LLM
        """
        tool_descriptions = []
        for tool in available_tools:
            if isinstance(tool, str):
                tool_descriptions.append(f"- {tool}")
            elif isinstance(tool, dict):
                # Format OpenAI tool definition
                name = tool.get("function", {}).get("name", tool.get("name", "unknown"))
                desc = tool.get("function", {}).get("description", tool.get("description", ""))
                # Extract parameters schema
                params_schema = tool.get("function", {}).get(
                    "parameters", tool.get("parameters", {})
                )

                # Format parameters for prompt
                params_desc = []
                if "properties" in params_schema:
                    for param_name, param_info in params_schema["properties"].items():
                        req = (
                            " (required)" if param_name in params_schema.get("required", []) else ""
                        )
                        p_desc = param_info.get("description", "")
                        params_desc.append(f"    - {param_name}: {p_desc}{req}")

                params_str = "\n".join(params_desc)
                tool_descriptions.append(f"- {name}: {desc}\n  Parameters:\n{params_str}")

        tool_info = (
            "\n".join(tool_descriptions) if tool_descriptions else "No specific tools required"
        )

        pattern_hint = ""
        if pattern:
            pattern_hint = f"\nThis request appears to follow a '{pattern.name}' pattern: {pattern.description}"

        return f"""Analyze this user request and decompose it into discrete tasks.

User Request: {user_request}
{pattern_hint}

Available Tools:
{tool_info}

For each task, provide:
1. id: A unique identifier (e.g., "search_1", "analyze_2")
2. type: One of "retrieve", "generate", "analyze", "transform"
3. tool: The tool to use (from available tools)
4. description: What this task does
5. parameters: Arguments for the tool (MUST match tool parameters schema)
6. dependencies: List of task IDs this depends on (empty if none)

Output as JSON array:
```json
[
  {{"id": "task_id", "type": "retrieve", "tool": "kb_search", "description": "...", "parameters": {{...}}, "dependencies": []}}
]
```

Rules:
- Tasks that can run in parallel should have no dependencies on each other
- A task can only depend on tasks defined before it
- Use the most appropriate tool for each task
- Keep tasks atomic and focused
- CRITICAL: Provide all required parameters for tools. For example, if using 'execute_python_code', you MUST provide the 'code' parameter.
"""

    async def _call_llm(self, prompt: str) -> str:
        """
        Call the LLM with the given prompt.

        Args:
            prompt: The prompt to send

        Returns:
            LLM response text
        """
        if not self.model_client:
            raise ValueError("No model client configured")

        # This is a simplified interface - actual implementation depends on client type
        # For Anthropic:
        if hasattr(self.model_client, "messages"):
            response = await self.model_client.messages.create(
                model=self.model_name,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        # For OpenAI-compatible:
        if hasattr(self.model_client, "chat"):
            response = await self.model_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )
            return response.choices[0].message.content

        raise ValueError("Unsupported model client type")

    def _parse_llm_response(
        self,
        response: str,
        available_tools: list[str],
    ) -> list[PlannedTask]:
        """
        Parse LLM response into PlannedTask objects.

        Args:
            response: LLM response text
            available_tools: Available tools for validation

        Returns:
            List of PlannedTask objects
        """
        tasks: list[PlannedTask] = []

        # Extract JSON from response
        json_match = re.search(r"```json\s*([\s\S]*?)\s*```", response)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find JSON array directly
            json_match = re.search(r"\[[\s\S]*\]", response)
            if json_match:
                json_str = json_match.group(0)
            else:
                return tasks

        try:
            task_data = json.loads(json_str)

            for item in task_data:
                task_type = TaskType(item.get("type", "retrieve"))
                tool = item.get("tool", "")

                # Validate tool is available
                if available_tools and tool not in available_tools:
                    alt_tool = self._find_alternative_tool(tool, available_tools)
                    if alt_tool:
                        tool = alt_tool

                task = PlannedTask(
                    id=item.get("id", f"task_{len(tasks) + 1}"),
                    type=task_type,
                    tool=tool,
                    description=item.get("description", ""),
                    parameters=item.get("parameters", {}),
                    dependencies=set(item.get("dependencies", [])),
                )
                tasks.append(task)

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse LLM response: {e}")

        return tasks

    def analyze_dependencies(self, tasks: list[PlannedTask]) -> list[list[str]]:
        """
        Analyze task dependencies and group tasks for parallel execution.

        Uses Kahn's algorithm for topological sorting to:
        1. Detect circular dependencies
        2. Determine execution order
        3. Group independent tasks for parallel execution

        Args:
            tasks: List of tasks with dependency information

        Returns:
            List of parallel groups (each group contains task IDs that can run together)

        Raises:
            CircularDependencyError: If circular dependencies are detected

        Example:
            ```python
            tasks = [
                PlannedTask(id="a", ..., dependencies=set()),
                PlannedTask(id="b", ..., dependencies=set()),
                PlannedTask(id="c", ..., dependencies={"a", "b"}),
            ]
            groups = planner.analyze_dependencies(tasks)
            # Returns: [["a", "b"], ["c"]]
            # "a" and "b" can run in parallel, "c" must wait
            ```
        """
        # Handle edge cases
        if not tasks:
            return []

        # Build adjacency list and in-degree count
        task_ids = {t.id for t in tasks}
        task_map = {t.id: t for t in tasks}

        # Check for self-dependencies (trivial cycles)
        for task in tasks:
            if task.id in task.dependencies:
                raise CircularDependencyError([task.id, task.id])

        # Handle single task case (after self-dependency check)
        if len(tasks) == 1:
            return [[tasks[0].id]]

        # Validate dependencies reference existing tasks
        for task in tasks:
            invalid_deps = task.dependencies - task_ids
            if invalid_deps:
                logger.warning(
                    f"Task {task.id} has dependencies on non-existent tasks: {invalid_deps}"
                )
                task.dependencies -= invalid_deps

        # Calculate in-degree for each task
        in_degree: dict[str, int] = {t.id: len(t.dependencies) for t in tasks}

        # Build reverse adjacency (who depends on whom)
        dependents: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            for dep in task.dependencies:
                dependents[dep].append(task.id)

        # Kahn's algorithm for topological sort with level tracking
        parallel_groups: list[list[str]] = []
        processed = set()

        while len(processed) < len(tasks):
            # Find all tasks with no remaining dependencies
            current_group = [
                tid for tid, degree in in_degree.items() if degree == 0 and tid not in processed
            ]

            if not current_group:
                # Find cycle - remaining tasks all have dependencies
                remaining = task_ids - processed
                cycle = self._find_cycle(task_map, remaining)
                raise CircularDependencyError(cycle)

            # Sort group by priority (higher priority first)
            current_group.sort(key=lambda tid: -task_map[tid].priority)

            parallel_groups.append(current_group)

            # Update in-degrees
            for tid in current_group:
                processed.add(tid)
                for dependent in dependents[tid]:
                    in_degree[dependent] -= 1

        return parallel_groups

    def _find_cycle(
        self,
        task_map: dict[str, PlannedTask],
        remaining: set[str],
    ) -> list[str]:
        """
        Find a cycle in the remaining tasks using DFS.

        Args:
            task_map: Map of task ID to task
            remaining: Set of remaining task IDs

        Returns:
            List of task IDs forming a cycle
        """
        visited = set()
        rec_stack = set()
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            task = task_map.get(node)
            if task:
                for dep in task.dependencies:
                    if dep in remaining:
                        if dep not in visited:
                            result = dfs(dep)
                            if result:
                                return result
                        elif dep in rec_stack:
                            # Found cycle
                            cycle_start = path.index(dep)
                            return path[cycle_start:] + [dep]

            path.pop()
            rec_stack.remove(node)
            return None

        for node in remaining:
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    return cycle

        return list(remaining)[:3]  # Fallback if cycle not found

    def validate_plan(self, plan: ExecutionPlan) -> tuple[bool, list[str]]:
        """
        Validate an execution plan for correctness.

        Checks:
        - All task dependencies exist
        - No circular dependencies
        - All tasks are in parallel groups
        - Tool assignments are valid

        Args:
            plan: The execution plan to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors: list[str] = []
        task_ids = {t.id for t in plan.tasks}

        # Check dependencies exist
        for task in plan.tasks:
            invalid_deps = task.dependencies - task_ids
            if invalid_deps:
                errors.append(f"Task {task.id} depends on non-existent tasks: {invalid_deps}")

        # Check for circular dependencies
        try:
            self.analyze_dependencies(plan.tasks)
        except CircularDependencyError as e:
            errors.append(str(e))

        # Check all tasks are in parallel groups
        grouped_ids = {tid for group in plan.parallel_groups for tid in group}
        ungrouped = task_ids - grouped_ids
        if ungrouped:
            errors.append(f"Tasks not in any parallel group: {ungrouped}")

        # Check for duplicate task IDs
        seen_ids = set()
        for task in plan.tasks:
            if task.id in seen_ids:
                errors.append(f"Duplicate task ID: {task.id}")
            seen_ids.add(task.id)

        return len(errors) == 0, errors


# =============================================================================
# Module-level convenience functions
# =============================================================================


def create_task_planner(
    model_client: Any | None = None,
    model_name: str | None = None,
) -> TaskPlanner:
    """
    Factory function to create a TaskPlanner instance.

    Args:
        model_client: Optional LLM client for intelligent planning
        model_name: Model identifier for LLM calls

    Returns:
        Configured TaskPlanner instance
    """
    return TaskPlanner(model_client=model_client, model_name=model_name)


def create_simple_plan(
    goal: str,
    tasks: list[dict[str, Any]],
) -> ExecutionPlan:
    """
    Create a simple execution plan from task dictionaries.

    Utility function for creating plans programmatically without
    using the full TaskPlanner.

    Args:
        goal: High-level goal description
        tasks: List of task dictionaries with keys:
               id, type, tool, description, parameters, dependencies

    Returns:
        ExecutionPlan with analyzed dependencies

    Example:
        ```python
        plan = create_simple_plan(
            goal="Search and answer",
            tasks=[
                {"id": "search", "type": "retrieve", "tool": "kb_search",
                 "description": "Search KB", "dependencies": []},
                {"id": "answer", "type": "generate", "tool": "generate_text",
                 "description": "Generate answer", "dependencies": ["search"]},
            ]
        )
        ```
    """
    planned_tasks = [PlannedTask.from_dict(t) for t in tasks]

    planner = TaskPlanner()
    parallel_groups = planner.analyze_dependencies(planned_tasks)

    return ExecutionPlan(
        goal=goal,
        tasks=planned_tasks,
        parallel_groups=parallel_groups,
    )
