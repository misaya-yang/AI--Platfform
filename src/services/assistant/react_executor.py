"""
ReAct Executor - Reasoning + Acting Agent Loop

Implements the ReAct paradigm for AI agents:
1. THINK: Reason about the task, share thinking with user
2. ACT: Execute tools or generate response
3. OBSERVE: Analyze results
4. UPDATE: Update working memory (todo.md pattern)

Based on Manus Context Engineering best practices:
- Append-only context for KV-cache efficiency
- todo.md pattern to prevent "lost in the middle" issues
- Error preservation for learning

References:
- Manus Context Engineering: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- OpenAI Agent Best Practices: https://developers.openai.com/tracks/building-agents/
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger
from ...models.enums import StreamEventType
from .working_memory import WorkingMemory, TaskStatus, TaskItem
from .task_planner import TaskPlanner, ExecutionPlan, PlannedTask

if TYPE_CHECKING:
    from .tools import ToolCallRequest, ToolCallResult

logger = get_logger(__name__)


class ReActPhase(str, Enum):
    """Current phase in ReAct loop."""
    ANALYZING = "analyzing"      # Initial task analysis
    THINKING = "thinking"        # Reasoning about next step
    PLANNING = "planning"        # Creating execution plan
    EXECUTING = "executing"      # Running a tool
    OBSERVING = "observing"      # Analyzing tool result
    WRITING = "writing"          # Generating content
    COMPLETING = "completing"    # Finishing up


@dataclass
class ReActState:
    """State tracking for ReAct execution."""
    phase: ReActPhase = ReActPhase.ANALYZING
    current_task_id: Optional[str] = None
    iteration: int = 0
    max_iterations: int = 10
    thinking_content: str = ""
    action_history: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class ReActEvent:
    """Event emitted during ReAct execution."""
    event_type: str
    data: Any
    timestamp: float = field(default_factory=time.time)


class ReActExecutor:
    """
    ReAct (Reasoning + Acting) Agent Executor.

    Implements the Think-Act-Observe-Update loop with:
    - Streaming thought output for user visibility
    - Working memory (todo.md pattern) for goal tracking
    - Error preservation for model learning
    - KV-cache friendly context management

    Usage:
        ```python
        executor = ReActExecutor(
            task_planner=planner,
            tool_executor=tool_registry.execute,
            llm_caller=model_client.stream,
        )

        async for event in executor.execute(
            user_message="帮我写一个项目计划书",
            available_tools=["generate_document", "kb_search"],
        ):
            if event.event_type == "text_delta":
                print(event.data, end="")
            elif event.event_type == "status":
                print(f"[{event.data['phase']}] {event.data.get('message', '')}")
        ```
    """

    def __init__(
        self,
        task_planner: Optional[TaskPlanner] = None,
        tool_executor: Optional[Callable] = None,
        llm_caller: Optional[Callable] = None,
        max_iterations: int = 10,
    ):
        """
        Initialize ReAct executor.

        Args:
            task_planner: TaskPlanner for decomposing complex requests
            tool_executor: Function to execute tools (async)
            llm_caller: Function to call LLM for reasoning (async generator)
            max_iterations: Maximum number of ReAct iterations
        """
        self.task_planner = task_planner
        self.tool_executor = tool_executor
        self.llm_caller = llm_caller
        self.max_iterations = max_iterations

    async def execute(
        self,
        user_message: str,
        available_tools: List[Any],
        context: Optional[Dict[str, Any]] = None,
        working_memory: Optional[WorkingMemory] = None,
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Execute user request using ReAct paradigm.

        Args:
            user_message: The user's request
            available_tools: List of available tools
            context: Additional context (history, preferences, etc.)
            working_memory: Optional existing working memory to continue from

        Yields:
            ReActEvent objects for each step in the process
        """
        context = context or {}

        # Generate run_id for this execution
        run_id = context.get("run_id") or uuid.uuid4().hex

        # Initialize state
        state = ReActState(max_iterations=self.max_iterations)

        # Initialize or continue working memory
        if working_memory is None:
            working_memory = WorkingMemory()
            working_memory.goal = user_message

        # === AG-UI: RUN_STARTED ===
        yield ReActEvent(
            event_type=StreamEventType.RUN_STARTED.value,
            data={
                "run_id": run_id,
                "timestamp": time.time(),
            }
        )

        # Phase 1: ANALYZE - Understand the task
        yield ReActEvent(
            event_type="status",
            data={
                "phase": ReActPhase.ANALYZING.value,
                "message": "分析任务需求...",
            }
        )

        # Detect if this is a document generation task
        is_document_task = self._is_document_generation_task(user_message)

        # Phase 2: PLAN - Create execution plan
        if self.task_planner and not is_document_task:
            yield ReActEvent(
                event_type="status",
                data={
                    "phase": ReActPhase.PLANNING.value,
                    "message": "制定执行计划...",
                }
            )

            try:
                plan = await self.task_planner.create_plan(
                    user_request=user_message,
                    available_tools=available_tools,
                    context=context,
                )

                # Update working memory with plan (using TaskItem objects)
                working_memory.tasks = [
                    TaskItem(id=t.id, description=t.description, status=TaskStatus.PENDING)
                    for t in plan.tasks
                ]

                # Emit task planning event
                yield ReActEvent(
                    event_type="task_planning",
                    data={
                        "goal": plan.goal,
                        "tasks": [t.to_dict() for t in plan.tasks],
                        "parallel_groups": plan.parallel_groups,
                    }
                )

                # Execute plan
                async for event in self._execute_plan(plan, working_memory, state, context):
                    yield event

            except Exception as e:
                logger.error(f"Planning failed: {e}")
                # Fall back to direct execution
                async for event in self._execute_direct(user_message, working_memory, state, context):
                    yield event
        else:
            # For document generation or simple tasks, use direct execution
            async for event in self._execute_direct(user_message, working_memory, state, context):
                yield event

        # Phase N: COMPLETE
        yield ReActEvent(
            event_type="status",
            data={
                "phase": ReActPhase.COMPLETING.value,
                "message": "任务完成",
            }
        )

        # === AG-UI: RUN_FINISHED ===
        yield ReActEvent(
            event_type=StreamEventType.RUN_FINISHED.value,
            data={
                "run_id": run_id,
                "timestamp": time.time(),
            }
        )

    def _is_document_generation_task(self, message: str) -> bool:
        """Check if the message requests document generation."""
        doc_keywords = [
            "写", "撰写", "生成文档", "写报告", "写计划", "写文章",
            "帮我写", "写一个", "写一份", "文档", "报告", "计划书",
            "write", "generate", "create", "document", "report", "plan",
            "docx", "pdf", "markdown",
        ]
        message_lower = message.lower()
        return any(kw in message_lower for kw in doc_keywords)

    async def _execute_plan(
        self,
        plan: ExecutionPlan,
        working_memory: WorkingMemory,
        state: ReActState,
        context: Dict[str, Any],
    ) -> AsyncGenerator[ReActEvent, None]:
        """Execute a structured plan with AG-UI compatible events."""
        now = time.time()

        for group_idx, group in enumerate(plan.parallel_groups):
            # Execute tasks in parallel group
            for task_id in group:
                task = plan.get_task(task_id)
                if not task:
                    continue

                state.current_task_id = task_id
                state.iteration += 1
                task_start_time = time.time()

                if state.iteration > state.max_iterations:
                    yield ReActEvent(
                        event_type=StreamEventType.RUN_ERROR.value,
                        data={
                            "run_id": context.get("run_id", "default"),
                            "error": f"达到最大迭代次数 ({state.max_iterations})",
                            "timestamp": time.time(),
                        }
                    )
                    return

                # === AG-UI: STEP_STARTED ===
                # Determine icon based on tool type
                tool_icon = self._get_tool_icon(task.tool) if task.tool else "brain"
                yield ReActEvent(
                    event_type=StreamEventType.STEP_STARTED.value,
                    data={
                        "step_id": task_id,
                        "title": task.description,
                        "description": f"执行工具: {task.tool}" if task.tool else None,
                        "icon": tool_icon,
                        "timestamp": time.time(),
                    }
                )

                # Update task status: in_progress
                self._update_task_status(working_memory, task_id, TaskStatus.IN_PROGRESS)
                yield ReActEvent(
                    event_type="working_memory_update",
                    data=working_memory.to_dict()
                )

                # === THINK Phase: Generate reasoning about the task ===
                yield ReActEvent(
                    event_type="status",
                    data={
                        "phase": ReActPhase.THINKING.value,
                        "message": f"思考: {task.description}",
                        "task_id": task_id,
                    }
                )

                # Use LLM to reason about the task (if available)
                if self.llm_caller:
                    async for event in self._think_about_task(task, working_memory, state, context):
                        yield event

                # ACT: Execute the task
                if task.tool and self.tool_executor:
                    # Legacy status event
                    yield ReActEvent(
                        event_type="status",
                        data={
                            "phase": ReActPhase.EXECUTING.value,
                            "message": f"执行: {task.tool}",
                            "task_id": task_id,
                        }
                    )

                    # === AG-UI: TOOL_CALL_START ===
                    tool_call_id = f"{task_id}_{uuid.uuid4().hex[:8]}"
                    yield ReActEvent(
                        event_type=StreamEventType.TOOL_CALL_START.value,
                        data={
                            "tool_call_id": tool_call_id,
                            "tool_name": task.tool,
                            "step_id": task_id,
                            "timestamp": time.time(),
                        }
                    )

                    # Legacy tool_call event for backwards compatibility
                    yield ReActEvent(
                        event_type="tool_call",
                        data={
                            "id": task_id,
                            "name": task.tool,
                            "arguments": task.parameters,
                            "status": "running",
                        }
                    )

                    try:
                        result = await self.tool_executor(
                            call_id=task_id,
                            tool_name=task.tool,
                            arguments=task.parameters,
                        )

                        tool_duration_ms = (time.time() - task_start_time) * 1000
                        success = result.success if hasattr(result, 'success') else True
                        result_data = result.result if hasattr(result, 'result') else str(result)

                        # === AG-UI: TOOL_CALL_END ===
                        yield ReActEvent(
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data={
                                "tool_call_id": tool_call_id,
                                "timestamp": time.time(),
                            }
                        )

                        # === AG-UI: TOOL_CALL_RESULT ===
                        yield ReActEvent(
                            event_type=StreamEventType.TOOL_CALL_RESULT.value,
                            data={
                                "tool_call_id": tool_call_id,
                                "result": result_data,
                                "success": success,
                                "duration_ms": tool_duration_ms,
                                "timestamp": time.time(),
                            }
                        )

                        # Legacy tool_result event
                        yield ReActEvent(
                            event_type="tool_result",
                            data={
                                "tool_call_id": task_id,
                                "name": task.tool,
                                "result": result_data,
                                "success": success,
                            }
                        )

                        # OBSERVE: Update status based on result
                        if success:
                            self._update_task_status(working_memory, task_id, TaskStatus.COMPLETED)
                        else:
                            self._update_task_status(working_memory, task_id, TaskStatus.FAILED)
                            error_msg = result.error if hasattr(result, 'error') else "Unknown error"
                            state.errors.append(f"Task {task_id}: {error_msg}")
                            working_memory.add_note(f"任务 {task_id} 失败: {error_msg}")

                    except Exception as e:
                        logger.error(f"Tool execution failed: {e}")
                        self._update_task_status(working_memory, task_id, TaskStatus.FAILED)
                        state.errors.append(f"Task {task_id}: {str(e)}")
                        working_memory.add_note(f"任务 {task_id} 异常: {str(e)}")

                        # AG-UI: TOOL_CALL_END (even on error)
                        yield ReActEvent(
                            event_type=StreamEventType.TOOL_CALL_END.value,
                            data={
                                "tool_call_id": tool_call_id,
                                "timestamp": time.time(),
                            }
                        )

                        yield ReActEvent(
                            event_type="tool_error",
                            data={
                                "tool_name": task.tool,
                                "tool_call_id": task_id,
                                "error_message": str(e),
                            }
                        )

                # === AG-UI: STEP_FINISHED ===
                step_duration_ms = (time.time() - task_start_time) * 1000
                step_status = "completed" if task_id not in [e.split(":")[0].replace("Task ", "") for e in state.errors] else "failed"
                yield ReActEvent(
                    event_type=StreamEventType.STEP_FINISHED.value,
                    data={
                        "step_id": task_id,
                        "status": step_status,
                        "duration_ms": step_duration_ms,
                        "timestamp": time.time(),
                    }
                )

                # UPDATE: Emit working memory update
                yield ReActEvent(
                    event_type="working_memory_update",
                    data=working_memory.to_dict()
                )

                # Record action in history
                state.action_history.append({
                    "task_id": task_id,
                    "tool": task.tool,
                    "iteration": state.iteration,
                })

    async def _stream_thinking(
        self,
        messages: List[Dict[str, Any]],
        task_id: str,
        max_tokens: int = 200,
        temperature: float = 0.7,
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Stream thinking content from LLM with unified event emission.

        This is the common helper for all thinking generation methods.
        Handles streaming, content extraction, and error handling.

        Args:
            messages: LLM messages to send
            task_id: Task identifier for event tracking
            max_tokens: Maximum tokens for response
            temperature: LLM temperature

        Yields:
            ReActEvent (thinking_start, thinking_delta, thinking_end, thinking_error)
        """
        if not self.llm_caller:
            return

        # Signal start of thinking
        yield ReActEvent(
            event_type="thinking_start",
            data={"task_id": task_id, "timestamp": time.time()}
        )

        thinking_content = ""
        try:
            # Stream from LLM
            async for delta in self.llm_caller(messages, max_tokens=max_tokens, temperature=temperature):
                content = ""
                if hasattr(delta, 'content') and delta.content:
                    content = delta.content
                elif isinstance(delta, dict) and delta.get('content'):
                    content = delta['content']
                elif isinstance(delta, str):
                    content = delta

                if content:
                    thinking_content += content
                    yield ReActEvent(
                        event_type="thinking_delta",
                        data={"content": content, "task_id": task_id}
                    )

            # Signal end of thinking
            yield ReActEvent(
                event_type="thinking_end",
                data={
                    "task_id": task_id,
                    "content": thinking_content,
                    "timestamp": time.time(),
                }
            )

        except Exception as e:
            logger.warning(f"Thinking generation failed for {task_id}: {e}")
            yield ReActEvent(
                event_type="thinking_error",
                data={"task_id": task_id, "error": str(e)}
            )

    async def _think_about_task(
        self,
        task: PlannedTask,
        working_memory: WorkingMemory,
        state: ReActState,
        context: Dict[str, Any],
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Generate LLM reasoning about the current task.

        This implements the THINK phase of ReAct:
        - Analyze what needs to be done
        - Consider context and constraints
        - Decide on approach
        - Stream thinking to user for visibility
        """
        if not self.llm_caller:
            return

        # Build thinking prompt
        thinking_prompt = self._build_thinking_prompt(task, working_memory, state, context)

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个智能助手的思考模块。在执行任务前，简洁分析任务要求并规划执行策略。"
                    "输出格式：用1-2句话说明你的理解和计划，保持简洁专业。"
                    "不要自我介绍，直接输出分析内容。"
                ),
            },
            {"role": "user", "content": thinking_prompt},
        ]

        # Use common streaming helper
        thinking_content = ""
        async for event in self._stream_thinking(messages, task.id, max_tokens=200, temperature=0.7):
            yield event
            # Capture content for working memory
            if event.event_type == "thinking_end":
                thinking_content = event.data.get("content", "")

        # Store thinking in state and working memory
        if thinking_content:
            state.thinking_content = thinking_content
            working_memory.add_note(f"[思考] {task.description}: {thinking_content[:200]}")

    def _build_thinking_prompt(
        self,
        task: PlannedTask,
        working_memory: WorkingMemory,
        state: ReActState,
        context: Dict[str, Any],
    ) -> str:
        """Build the prompt for task reasoning."""
        parts = []

        # Current goal
        if working_memory.goal:
            parts.append(f"**用户目标:** {working_memory.goal}")

        # Current task
        parts.append(f"\n**当前任务:** {task.description}")
        if task.tool:
            parts.append(f"**计划工具:** {task.tool}")
        if task.parameters:
            params_str = ", ".join(f"{k}={v}" for k, v in task.parameters.items())
            parts.append(f"**工具参数:** {params_str}")

        # Progress
        completed_tasks = [t for t in working_memory.tasks if t.status == TaskStatus.COMPLETED]
        if completed_tasks:
            completed_names = [t.description or t.id for t in completed_tasks]
            parts.append(f"\n**已完成:** {', '.join(completed_names)}")

        # Previous errors
        if state.errors:
            parts.append(f"\n**注意前序错误:** {'; '.join(state.errors[-2:])}")

        # RAG context (if any)
        rag_context = context.get("rag_context")
        if rag_context:
            parts.append(f"\n**参考知识:**\n{rag_context[:500]}...")

        parts.append("\n请简要分析任务并说明执行计划。")

        return "\n".join(parts)

    def _get_tool_icon(self, tool_name: str) -> str:
        """Map tool name to icon for frontend display."""
        tool_icons = {
            "kb_search": "kb",
            "knowledge_base_search": "kb",
            "web_search": "web",
            "search_web": "web",
            "generate_image": "image",
            "image_generation": "image",
            "generate_document": "doc",
            "document_generation": "doc",
            "generate_pptx": "ppt",
            "pptx_generator": "ppt",
            "code_execution": "code",
            "execute_code": "code",
            "file_read": "file",
            "file_write": "file",
        }
        return tool_icons.get(tool_name, "brain")

    async def _execute_direct(
        self,
        user_message: str,
        working_memory: WorkingMemory,
        state: ReActState,
        context: Dict[str, Any],
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Direct execution for document generation and simple tasks.

        Implements the two-stage document generation flow:
        1. THINK: Generate reasoning and content strategy
        2. ACT: Execute or generate response
        """
        is_doc_task = self._is_document_generation_task(user_message)

        if is_doc_task:
            # Document generation: Two-stage flow

            # Stage 1: THINK - Analyze requirements
            yield ReActEvent(
                event_type="status",
                data={
                    "phase": ReActPhase.THINKING.value,
                    "message": "分析文档需求...",
                }
            )

            # Use LLM to reason about document requirements
            if self.llm_caller:
                async for event in self._think_about_document(user_message, context):
                    yield event

            # Add task to working memory (using TaskItem objects)
            working_memory.tasks = [
                TaskItem(id="analyze", description="分析需求", status=TaskStatus.COMPLETED),
                TaskItem(id="write", description="撰写内容", status=TaskStatus.IN_PROGRESS),
                TaskItem(id="generate", description="生成文档", status=TaskStatus.PENDING),
            ]
            yield ReActEvent(
                event_type="working_memory_update",
                data=working_memory.to_dict()
            )

            yield ReActEvent(
                event_type="status",
                data={
                    "phase": ReActPhase.WRITING.value,
                    "message": "撰写文档内容...",
                }
            )

            # Update task status
            self._update_task_status(working_memory, "write", TaskStatus.COMPLETED)
            yield ReActEvent(
                event_type="working_memory_update",
                data=working_memory.to_dict()
            )

            # Stage 2: ACT - Will be handled when tool is called
            self._update_task_status(working_memory, "generate", TaskStatus.IN_PROGRESS)
            yield ReActEvent(
                event_type="status",
                data={
                    "phase": ReActPhase.EXECUTING.value,
                    "message": "准备生成文档文件...",
                }
            )
            yield ReActEvent(
                event_type="working_memory_update",
                data=working_memory.to_dict()
            )
        else:
            # Simple task: Direct response with thinking
            yield ReActEvent(
                event_type="status",
                data={
                    "phase": ReActPhase.THINKING.value,
                    "message": "思考回答...",
                }
            )

            # Use LLM to reason about simple tasks too
            if self.llm_caller:
                async for event in self._think_about_simple_task(user_message, context):
                    yield event

    async def _think_about_document(
        self,
        user_message: str,
        context: Dict[str, Any],
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Generate thinking about document requirements.

        Args:
            user_message: The user's document request
            context: Additional context

        Yields:
            ReActEvent with thinking content
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "你是文档助手的分析模块。分析用户的文档需求，简要说明：\n"
                    "1. 文档类型和目的\n"
                    "2. 关键内容要点\n"
                    "3. 结构建议\n"
                    "用2-3句话概括，保持简洁专业。"
                ),
            },
            {"role": "user", "content": f"用户请求: {user_message}"},
        ]

        # Use common streaming helper
        async for event in self._stream_thinking(messages, "document_analysis", max_tokens=150, temperature=0.5):
            yield event

    async def _think_about_simple_task(
        self,
        user_message: str,
        context: Dict[str, Any],
    ) -> AsyncGenerator[ReActEvent, None]:
        """
        Generate thinking about simple tasks.

        Args:
            user_message: The user's request
            context: Additional context

        Yields:
            ReActEvent with thinking content
        """
        # Check if RAG context is available
        rag_context = context.get("rag_context", "")
        rag_hint = f"\n参考知识库内容: {rag_context[:300]}..." if rag_context else ""

        messages = [
            {
                "role": "system",
                "content": (
                    "你是智能助手的思考模块。简要分析用户问题，说明你的理解和回答策略。"
                    "用1-2句话，保持简洁。"
                ),
            },
            {"role": "user", "content": f"用户问题: {user_message}{rag_hint}"},
        ]

        # Use common streaming helper
        async for event in self._stream_thinking(messages, "simple_task", max_tokens=100, temperature=0.5):
            yield event

    def _update_task_status(
        self,
        working_memory: WorkingMemory,
        task_id: str,
        status: TaskStatus,
    ):
        """Update task status in working memory."""
        for task in working_memory.tasks:
            if task.id == task_id:
                task.status = status
                break


def create_react_executor(
    task_planner: Optional[TaskPlanner] = None,
    tool_executor: Optional[Callable] = None,
    llm_caller: Optional[Callable] = None,
    max_iterations: int = 10,
) -> ReActExecutor:
    """
    Factory function to create a ReActExecutor.

    Args:
        task_planner: Optional TaskPlanner for complex requests
        tool_executor: Function to execute tools
        llm_caller: Function to call LLM
        max_iterations: Maximum iterations

    Returns:
        Configured ReActExecutor instance
    """
    return ReActExecutor(
        task_planner=task_planner,
        tool_executor=tool_executor,
        llm_caller=llm_caller,
        max_iterations=max_iterations,
    )
