"""
Tool Orchestrator - Parallel Tool Execution with Dependency Coordination.

This module provides the ToolOrchestrator class that executes tools with
parallel support and dependency coordination. It works with TaskPlanner
to execute planned tasks efficiently.

Key Features:
- Execute planned tasks in parallel groups
- Respect dependencies between tasks
- Yield results as they complete using async generators
- Update working memory with task status
- Limit concurrent executions with semaphore
- Resolve parameter references between tasks
- Unified tool execution via ToolInvoker abstraction

Usage:
    ```python
    # Using ToolInvoker (recommended)
    orchestrator = ToolOrchestrator(tool_invoker=invoker, max_parallel=5)

    # Using ToolRegistry (backward compatible)
    orchestrator = ToolOrchestrator(tool_registry=registry, max_parallel=5)

    async for result in orchestrator.execute_plan(plan, working_memory):
        print(f"Task {result.task_id} completed: {result.success}")
        if result.error:
            print(f"Error: {result.error}")
    ```

References:
- asyncio.as_completed: https://docs.python.org/3/library/asyncio-task.html#asyncio.as_completed
- Semaphore pattern: https://docs.python.org/3/library/asyncio-sync.html#asyncio.Semaphore
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...core.observability.logging import get_logger
from .tasks.task_planner import ExecutionPlan, PlannedTask
from .working_memory import TaskStatus, WorkingMemory

if TYPE_CHECKING:
    from .gateway import AssistantExecutionGateway, RoutedAssistantRequest
    from .tool_invoker import ToolInvocationContext, ToolInvoker
    from .tools.tool_registry import ToolRegistry

logger = get_logger(__name__)


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ToolExecutionResult:
    """
    Result of a single tool execution within the orchestrator.

    This dataclass captures the outcome of executing a planned task,
    including timing information for performance monitoring.

    Attributes:
        task_id: Unique identifier of the task that was executed
        tool: Name of the tool that was called
        success: Whether the execution completed successfully
        result: The result returned by the tool (if successful)
        error: Error message if the execution failed
        duration_ms: Time taken to execute the tool in milliseconds

    Example:
        ```python
        result = ToolExecutionResult(
            task_id="search_1",
            tool="kb_search",
            success=True,
            result={"documents": [...]},
            duration_ms=150.5
        )
        ```
    """

    task_id: str
    tool: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize result to dictionary for JSON serialization.

        Returns:
            Dictionary representation of the execution result
        """
        return {
            "task_id": self.task_id,
            "tool": self.tool,
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolExecutionResult:
        """
        Deserialize result from dictionary.

        Args:
            data: Dictionary containing result data

        Returns:
            ToolExecutionResult instance
        """
        return cls(
            task_id=data["task_id"],
            tool=data["tool"],
            success=data["success"],
            result=data.get("result"),
            error=data.get("error"),
            duration_ms=data.get("duration_ms", 0),
        )


# =============================================================================
# Tool Orchestrator
# =============================================================================


class ToolOrchestrator:
    """
    Executes tools with parallel support and dependency coordination.

    The ToolOrchestrator takes an ExecutionPlan from TaskPlanner and
    executes the tasks respecting dependencies and parallelization.
    It uses a semaphore to limit concurrent executions and yields
    results as they complete for responsive feedback.

    Key responsibilities:
    1. Process parallel_groups sequentially (groups must complete in order)
    2. Execute tasks within each group in parallel
    3. Resolve parameter references like ${task_id.result}
    4. Update WorkingMemory with task status
    5. Track execution timing for performance monitoring

    Usage:
        ```python
        # Recommended: Using ToolInvoker for unified execution
        from src.services.assistant.tool_invoker import create_tool_invoker

        orchestrator = ToolOrchestrator(
            tool_invoker=create_tool_invoker(),
            max_parallel=5
        )

        # Backward compatible: Using ToolRegistry directly
        from src.services.assistant.tools.tool_registry import get_tool_registry

        orchestrator = ToolOrchestrator(
            tool_registry=get_tool_registry(),
            max_parallel=5
        )

        async for result in orchestrator.execute_plan(plan, working_memory):
            if result.success:
                print(f"Completed: {result.task_id}")
            else:
                print(f"Failed: {result.task_id} - {result.error}")
        ```

    Thread Safety:
        The ToolOrchestrator uses asyncio primitives and is safe for
        concurrent use within the same event loop. Each execute_plan
        call operates independently.
    """

    # Pattern for matching parameter references like ${task_id.result} or ${task_id.field}
    PARAM_REF_PATTERN = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\.([\w.]+)\}")

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        tool_invoker: ToolInvoker | None = None,
        max_parallel: int = 5,
        invocation_context: ToolInvocationContext | None = None,
        execution_gateway: AssistantExecutionGateway | None = None,
        routed_request: RoutedAssistantRequest | None = None,
    ):
        """
        Initialize the ToolOrchestrator.

        Args:
            tool_registry: Registry containing tool definitions and executors (legacy)
            tool_invoker: ToolInvoker for unified execution (recommended)
            max_parallel: Maximum number of concurrent tool executions (default 5)
            invocation_context: Default context for tool invocations

        Note:
            Either tool_invoker or tool_registry must be provided.
            If both are provided, tool_invoker takes precedence.
            If neither is provided, a default invoker will be created.
        """
        self.tool_invoker = tool_invoker
        self.tool_registry = tool_registry
        self.max_parallel = max_parallel
        self._semaphore: asyncio.Semaphore | None = None
        self._default_invocation_context = invocation_context
        self._execution_gateway = execution_gateway
        self._routed_request = routed_request

        # If no invoker provided, create one from registry or default
        if self.tool_invoker is None:
            from .tool_invoker import create_tool_invoker

            self.tool_invoker = create_tool_invoker(tool_registry=tool_registry)

        logger.info(f"ToolOrchestrator initialized with max_parallel={max_parallel}")

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Lazy-initialize the semaphore inside the running event loop.

        Creating an ``asyncio.Semaphore`` before an event loop is active
        (e.g. at module-import time or in ``__init__`` of a long-lived
        singleton) can bind it to the wrong loop and cause
        "got Future attached to a different loop" errors.  By deferring
        creation to first access we guarantee the semaphore belongs to the
        loop that is actually executing the coroutines.
        """
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_parallel)
        return self._semaphore

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        working_memory: WorkingMemory,
        invocation_context: ToolInvocationContext | None = None,
    ) -> AsyncGenerator[ToolExecutionResult, None]:
        """
        Execute plan respecting dependencies, yielding results as they complete.

        Processes parallel_groups sequentially - each group must complete
        before the next group starts. Within each group, tasks are executed
        in parallel up to max_parallel concurrent executions.

        Args:
            plan: The execution plan containing tasks and parallel groups
            working_memory: Working memory to update with task status
            invocation_context: Context for tool invocations (session info, etc.)

        Yields:
            ToolExecutionResult for each completed task

        Example:
            ```python
            async for result in orchestrator.execute_plan(plan, memory, context):
                print(f"Task {result.task_id}: {'success' if result.success else 'failed'}")
            ```
        """
        # Use provided context or default
        ctx = invocation_context or self._default_invocation_context
        logger.info(
            f"Starting plan execution: {plan.goal} "
            f"({len(plan.tasks)} tasks in {len(plan.parallel_groups)} groups)"
        )

        # Track results from all completed tasks for parameter resolution
        prior_results: dict[str, ToolExecutionResult] = {}

        # Set the goal in working memory
        if plan.goal:
            working_memory.set_goal(plan.goal)

        # Add all tasks to working memory
        for task in plan.tasks:
            working_memory.add_task(task.id, task.description)

        # Process each parallel group sequentially
        for group_index, group_task_ids in enumerate(plan.parallel_groups):
            logger.debug(
                f"Processing group {group_index + 1}/{len(plan.parallel_groups)}: {group_task_ids}"
            )

            # Get the PlannedTask objects for this group
            tasks_in_group = [
                plan.get_task(task_id)
                for task_id in group_task_ids
                if plan.get_task(task_id) is not None
            ]

            if not tasks_in_group:
                logger.warning(f"No valid tasks found in group {group_index + 1}")
                continue

            # Execute all tasks in this group in parallel and yield results
            async for result in self._execute_parallel(
                tasks_in_group,
                prior_results,
                working_memory,
                ctx,
            ):
                # Store result for parameter resolution in later groups
                prior_results[result.task_id] = result

                # Update working memory with completion status
                status = TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
                result_summary = str(result.result)[:200] if result.result else None
                working_memory.update_task(
                    result.task_id,
                    status,
                    result=result_summary,
                    error=result.error,
                )

                yield result

        # Log summary
        completed_count = sum(1 for r in prior_results.values() if r.success)
        failed_count = sum(1 for r in prior_results.values() if not r.success)
        logger.info(
            f"Plan execution complete: {completed_count} succeeded, "
            f"{failed_count} failed out of {len(prior_results)} tasks"
        )

    async def _execute_parallel(
        self,
        tasks: list[PlannedTask],
        prior_results: dict[str, ToolExecutionResult],
        working_memory: WorkingMemory,
        invocation_context: ToolInvocationContext | None = None,
    ) -> AsyncGenerator[ToolExecutionResult, None]:
        """
        Execute a group of tasks in parallel using asyncio.as_completed.

        Uses the semaphore to limit concurrent executions and yields
        results as soon as they complete (not in submission order).

        Args:
            tasks: List of tasks to execute in parallel
            prior_results: Results from previously completed tasks (for param resolution)
            working_memory: Working memory to update with task status
            invocation_context: Context for tool invocations

        Yields:
            ToolExecutionResult for each completed task
        """
        if not tasks:
            return

        # Create a task for each planned task
        # We need to map asyncio.Task back to PlannedTask for result association
        async_tasks: dict[asyncio.Task, PlannedTask] = {}

        for planned_task in tasks:
            # Mark task as in progress
            working_memory.update_task(planned_task.id, TaskStatus.IN_PROGRESS)

            # Create the coroutine with semaphore protection
            coro = self._execute_single_task(planned_task, prior_results, invocation_context)
            async_task = asyncio.create_task(coro)
            async_tasks[async_task] = planned_task

        # Use as_completed to yield results as they finish
        # Note: We iterate using the tasks directly to preserve the mapping
        for coro in asyncio.as_completed(list(async_tasks.keys())):
            try:
                result = await coro
                yield result
            except Exception as e:
                # Find which planned task this was for using O(1) lookup
                # Note: as_completed may wrap the original task, so we need
                # to find by matching. Since exceptions are rare and the dict
                # is typically small, this is acceptable.
                planned_task = async_tasks.get(coro)

                # Fallback to linear search if direct lookup fails
                # (as_completed may return wrapped futures)
                if planned_task is None:
                    for task, pt in async_tasks.items():
                        if task is coro or (
                            hasattr(task, "_coro") and task._coro is getattr(coro, "_coro", None)
                        ):
                            planned_task = pt
                            break

                # This shouldn't happen as _execute_single_task catches exceptions
                # but handle it just in case
                task_id = planned_task.id if planned_task else "unknown"
                tool_name = planned_task.tool if planned_task else "unknown"

                logger.error(f"Unexpected error in task {task_id}: {e}")
                yield ToolExecutionResult(
                    task_id=task_id,
                    tool=tool_name,
                    success=False,
                    error=f"Unexpected error: {str(e)}",
                    duration_ms=0,
                )

    async def _execute_single_task(
        self,
        task: PlannedTask,
        prior_results: dict[str, ToolExecutionResult],
        invocation_context: ToolInvocationContext | None = None,
    ) -> ToolExecutionResult:
        """
        Execute a single task with semaphore protection.

        Uses ToolInvoker for unified execution with support for:
        - Timeout handling
        - Retry logic
        - Metrics collection

        Args:
            task: The planned task to execute
            prior_results: Results from previously completed tasks
            invocation_context: Context for tool invocation

        Returns:
            ToolExecutionResult with execution outcome
        """
        start_time = time.time()

        async with self.semaphore:
            try:
                # Resolve parameter references
                resolved_params = self._resolve_params(task.parameters, prior_results)

                logger.debug(
                    f"Executing task {task.id}: tool={task.tool}, params={resolved_params}"
                )

                # Execute via ToolInvoker (unified execution layer)
                if invocation_context is None:
                    raise ValueError(
                        f"invocation_context is required for task {task.id}; "
                        "tool execution without tenant/user context is forbidden."
                    )

                if self._execution_gateway and self._execution_gateway.enabled:
                    tool_result = await self._execution_gateway.invoke_tool(
                        tool_name=task.tool,
                        arguments=resolved_params,
                        context=invocation_context,
                        routed_request=self._routed_request,
                    )
                else:
                    tool_result = await self.tool_invoker.invoke(
                        tool_name=task.tool,
                        arguments=resolved_params,
                        context=invocation_context,
                    )

                duration_ms = (time.time() - start_time) * 1000

                return ToolExecutionResult(
                    task_id=task.id,
                    tool=task.tool,
                    success=tool_result.success,
                    result=tool_result.result,
                    error=tool_result.error,
                    duration_ms=duration_ms,
                )

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                logger.error(f"Task {task.id} failed with exception: {e}")

                return ToolExecutionResult(
                    task_id=task.id,
                    tool=task.tool,
                    success=False,
                    error=str(e),
                    duration_ms=duration_ms,
                )

    def _resolve_params(
        self,
        params: dict[str, Any],
        prior_results: dict[str, ToolExecutionResult],
    ) -> dict[str, Any]:
        """
        Resolve parameter references like ${task_1.result} in task parameters.

        Supports references to prior task results using the format:
        - ${task_id.result} - Get the full result of a prior task
        - ${task_id.error} - Get the error message (if any)
        - ${task_id.success} - Get the success boolean
        - ${task_id.result.field} - Get a nested field from the result

        Args:
            params: Dictionary of parameters that may contain references
            prior_results: Results from previously completed tasks

        Returns:
            Dictionary with all references resolved to actual values

        Example:
            ```python
            params = {
                "query": "Analyze this: ${search_1.result}",
                "context": "${search_2.result.documents}"
            }
            resolved = orchestrator._resolve_params(params, prior_results)
            # Returns params with ${...} replaced by actual values
            ```
        """
        resolved: dict[str, Any] = {}

        for key, value in params.items():
            resolved[key] = self._resolve_value(value, prior_results)

        return resolved

    _MAX_RESOLVE_DEPTH: int = 10

    def _resolve_value(
        self,
        value: Any,
        prior_results: dict[str, ToolExecutionResult],
        _depth: int = 0,
    ) -> Any:
        """
        Recursively resolve references in a value.

        Args:
            value: The value to resolve (can be string, dict, list, or other)
            prior_results: Results from previously completed tasks
            _depth: Current recursion depth (internal use only)

        Returns:
            The resolved value

        Raises:
            ValueError: If recursion exceeds ``_MAX_RESOLVE_DEPTH`` (likely a
                circular reference in tool parameters).
        """
        if _depth > self._MAX_RESOLVE_DEPTH:
            raise ValueError(
                f"Circular dependency detected in tool parameters (depth > {self._MAX_RESOLVE_DEPTH})"
            )

        if isinstance(value, str):
            return self._resolve_string(value, prior_results)
        elif isinstance(value, dict):
            return {
                k: self._resolve_value(v, prior_results, _depth + 1)
                for k, v in value.items()
            }
        elif isinstance(value, list):
            return [
                self._resolve_value(item, prior_results, _depth + 1)
                for item in value
            ]
        else:
            # For non-string primitives (int, float, bool, None), return as-is
            return value

    def _resolve_string(
        self,
        value: str,
        prior_results: dict[str, ToolExecutionResult],
    ) -> Any:
        """
        Resolve parameter references in a string value.

        If the entire string is a single reference (e.g., "${task_1.result}"),
        returns the actual value (which may not be a string).

        If the string contains embedded references (e.g., "Query: ${task_1.result}"),
        returns a string with references replaced by their string representations.

        Args:
            value: String that may contain references
            prior_results: Results from previously completed tasks

        Returns:
            The resolved value (may be any type if entire string is a reference)
        """
        # Check if the entire string is a single reference
        single_ref_match = re.fullmatch(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\.([\w.]+)\}", value)
        if single_ref_match:
            task_id = single_ref_match.group(1)
            field_path = single_ref_match.group(2)
            resolved = self._get_reference_value(task_id, field_path, prior_results)
            # Return the actual value (may not be a string)
            return resolved if resolved is not None else value

        # Replace all references in the string with their string representations
        def replace_ref(match: re.Match) -> str:
            task_id = match.group(1)
            field_path = match.group(2)
            resolved = self._get_reference_value(task_id, field_path, prior_results)
            if resolved is None:
                return match.group(0)  # Keep original if not found
            return str(resolved)

        return self.PARAM_REF_PATTERN.sub(replace_ref, value)

    def _get_reference_value(
        self,
        task_id: str,
        field_path: str,
        prior_results: dict[str, ToolExecutionResult],
    ) -> Any:
        """
        Get the value for a reference like task_id.field_path.

        Args:
            task_id: ID of the task to get the result from
            field_path: Path to the field (e.g., "result", "result.documents")
            prior_results: Results from previously completed tasks

        Returns:
            The referenced value, or None if not found
        """
        result = prior_results.get(task_id)
        if result is None:
            logger.warning(f"Reference to unknown task: {task_id}")
            return None

        # Split field path and navigate
        fields = field_path.split(".")
        current = result

        for field in fields:
            if hasattr(current, field):
                current = getattr(current, field)
            elif isinstance(current, dict) and field in current:
                current = current[field]
            else:
                logger.warning(
                    f"Cannot resolve field '{field}' in reference ${{{task_id}.{field_path}}}"
                )
                return None

        return current


# =============================================================================
# Factory Function
# =============================================================================


def create_tool_orchestrator(
    tool_registry: ToolRegistry | None = None,
    tool_invoker: ToolInvoker | None = None,
    max_parallel: int = 5,
    invocation_context: ToolInvocationContext | None = None,
    execution_gateway: AssistantExecutionGateway | None = None,
    routed_request: RoutedAssistantRequest | None = None,
) -> ToolOrchestrator:
    """
    Factory function to create a ToolOrchestrator instance.

    Args:
        tool_registry: Tool registry to use (legacy, for backward compatibility)
        tool_invoker: ToolInvoker to use (recommended for new code)
        max_parallel: Maximum concurrent tool executions (default 5)
        invocation_context: Default context for tool invocations

    Returns:
        Configured ToolOrchestrator instance

    Example:
        ```python
        # Recommended: Using ToolInvoker
        from src.services.assistant.tool_invoker import create_tool_invoker
        orchestrator = create_tool_orchestrator(
            tool_invoker=create_tool_invoker(),
            max_parallel=10
        )

        # Backward compatible: Using ToolRegistry
        orchestrator = create_tool_orchestrator(max_parallel=10)
        ```
    """
    return ToolOrchestrator(
        tool_registry=tool_registry,
        tool_invoker=tool_invoker,
        max_parallel=max_parallel,
        invocation_context=invocation_context,
        execution_gateway=execution_gateway,
        routed_request=routed_request,
    )
