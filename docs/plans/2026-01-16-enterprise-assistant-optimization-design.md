# Enterprise AI Assistant Optimization Design

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现有AI助手升级为企业级智能工作助手，具备深度任务理解、持久工作记忆、并行能力调用和高性能响应能力。

**Architecture:** 新增智能编排层(Task Planner + Context Engine + Tool Orchestrator)和记忆持久层(Working Memory + Task State + Artifact Store)，让Agent从"单轮问答"升级为"持续工作伙伴"。

**Tech Stack:** Python/FastAPI (Backend), React/TypeScript (Frontend), PostgreSQL (Persistence), asyncio (Concurrency)

**References:**
- [Manus Context Engineering Blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- Enterprise AI Agent Architecture Best Practices 2025

---

## 1. Architecture Overview

### 1.1 Four-Layer Architecture

```
┌─────────────────────────────────────────────────────┐
│                   用户界面层                          │
│  Chat UI + Task Panel + Artifact Workspace          │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│                 智能编排层 (新增)                      │
│  Task Planner → Context Engine → Tool Orchestrator  │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│                   能力执行层                          │
│  KB Search | Web Search | Code Exec | Doc Gen | ... │
└─────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────┐
│                   记忆持久层 (新增)                    │
│  Working Memory | Task State | Artifact Store       │
└─────────────────────────────────────────────────────┘
```

### 1.2 Core Components

| Component | Responsibility | New/Existing |
|-----------|---------------|--------------|
| Task Planner | 任务分解、依赖分析、执行计划生成 | New |
| Context Engine | KV-Cache优化、prompt结构管理、记忆压缩 | New |
| Tool Orchestrator | 并行工具执行、依赖协调、结果聚合 | Enhanced |
| Working Memory | todo.md模式、任务状态追踪 | New |
| Session Memory | 对话历史、Artifacts、任务记录 | Enhanced |
| Long-term Memory | 用户偏好、历史模式 | New |

---

## 2. Context Engine Design

### 2.1 KV-Cache Optimization Strategy

**Problem:** 当前每次请求都重新处理完整prompt，浪费算力和时间。Claude cached tokens成本仅为uncached的1/10。

**Solution:** 稳定前缀设计，确保cache命中

```python
# src/services/assistant/context_engine.py

from dataclasses import dataclass
from typing import List, Dict, Any, Optional

@dataclass
class ContextStructure:
    """
    Stable prefix design for KV-Cache optimization.
    Order matters - most stable at top, most volatile at bottom.
    """
    # Layer 1: Static (never changes) - highest cache hit
    system_prompt: str
    tool_definitions: List[Dict[str, Any]]

    # Layer 2: User-level (changes rarely)
    user_preferences: Optional[str] = None
    long_term_memory: Optional[str] = None

    # Layer 3: Session-level (append-only)
    task_state: Optional[str] = None  # todo.md content
    conversation_history: List[Dict[str, Any]] = None

    # Layer 4: Request-level (changes every time)
    current_context: Optional[str] = None  # KB results, web search
    current_query: str = ""

class ContextEngine:
    """
    Manages context construction with KV-Cache optimization.
    Based on Manus Context Engineering principles.
    """

    # Cache breakpoint markers for providers that support explicit caching
    CACHE_BREAKPOINTS = {
        "anthropic": {"cache_control": {"type": "ephemeral"}},
        "openai": None,  # Uses automatic prefix caching
    }

    def __init__(self, provider: str):
        self.provider = provider

    def build_messages(self, context: ContextStructure) -> List[Dict[str, Any]]:
        """
        Build messages array with stable prefix for cache optimization.

        Key principles (from Manus):
        1. No timestamps at prompt start - destroys all cache
        2. Tool definitions in stable order - use masking instead of removal
        3. Conversation history append-only - never modify sent messages
        """
        messages = []

        # System message with stable content
        system_content = self._build_system_content(context)
        messages.append({
            "role": "system",
            "content": system_content
        })

        # Add cache breakpoint after system (for Anthropic)
        if self.provider == "anthropic":
            messages[-1]["cache_control"] = {"type": "ephemeral"}

        # Append conversation history (never modify)
        if context.conversation_history:
            messages.extend(context.conversation_history)

        # Current query (always changes)
        if context.current_query:
            user_content = context.current_query
            if context.current_context:
                user_content = f"{context.current_context}\n\n{context.current_query}"
            messages.append({
                "role": "user",
                "content": user_content
            })

        return messages

    def _build_system_content(self, context: ContextStructure) -> str:
        """Build stable system prompt content."""
        parts = [context.system_prompt]

        if context.user_preferences:
            parts.append(f"\n## User Preferences\n{context.user_preferences}")

        if context.long_term_memory:
            parts.append(f"\n## Background Knowledge\n{context.long_term_memory}")

        if context.task_state:
            parts.append(f"\n## Current Task State\n{context.task_state}")

        return "\n".join(parts)
```

### 2.2 Working Memory with todo.md Pattern

**Problem:** 复杂任务中Agent容易"忘记"最初目标，出现目标漂移。

**Solution:** 引入todo.md文件作为注意力锚点，利用"近因效应"维持任务聚焦。

```python
# src/services/assistant/working_memory.py

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum
from datetime import datetime

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"

@dataclass
class TaskItem:
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

@dataclass
class CollectedInfo:
    key: str
    value: str
    source: str  # tool name or "user"
    timestamp: datetime = field(default_factory=datetime.now)

class WorkingMemory:
    """
    Maintains task state and collected information during complex workflows.
    Rendered as todo.md format for injection into context.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.goal: Optional[str] = None
        self.tasks: List[TaskItem] = []
        self.collected_info: List[CollectedInfo] = []
        self.notes: List[str] = []

    def set_goal(self, goal: str) -> None:
        """Set the high-level goal for this task."""
        self.goal = goal

    def add_task(self, task_id: str, description: str) -> TaskItem:
        """Add a new task to the plan."""
        task = TaskItem(id=task_id, description=description)
        self.tasks.append(task)
        return task

    def update_task(self, task_id: str, status: TaskStatus,
                    result: Optional[str] = None,
                    error: Optional[str] = None) -> None:
        """Update task status and result."""
        for task in self.tasks:
            if task.id == task_id:
                task.status = status
                task.result = result
                task.error = error
                if status == TaskStatus.COMPLETED:
                    task.completed_at = datetime.now()
                break

    def add_info(self, key: str, value: str, source: str) -> None:
        """Record collected information."""
        self.collected_info.append(CollectedInfo(
            key=key, value=value, source=source
        ))

    def add_note(self, note: str) -> None:
        """Add a note or observation."""
        self.notes.append(note)

    def to_markdown(self) -> str:
        """
        Render working memory as todo.md format.
        Placed at end of context to leverage recency effect.
        """
        lines = ["# Current Task State", ""]

        if self.goal:
            lines.append(f"**Goal:** {self.goal}")
            lines.append("")

        # Task list with status indicators
        if self.tasks:
            lines.append("## Tasks")
            current_task = None
            for task in self.tasks:
                if task.status == TaskStatus.COMPLETED:
                    indicator = "[x]"
                elif task.status == TaskStatus.IN_PROGRESS:
                    indicator = "[~]"
                    current_task = task.description
                elif task.status == TaskStatus.FAILED:
                    indicator = "[!]"
                elif task.status == TaskStatus.BLOCKED:
                    indicator = "[B]"
                else:
                    indicator = "[ ]"

                line = f"- {indicator} {task.description}"
                if task.status == TaskStatus.IN_PROGRESS:
                    line += " <- current"
                if task.error:
                    line += f" (error: {task.error})"
                lines.append(line)
            lines.append("")

        # Collected information
        if self.collected_info:
            lines.append("## Collected Information")
            for info in self.collected_info:
                lines.append(f"- **{info.key}**: {info.value}")
            lines.append("")

        # Notes
        if self.notes:
            lines.append("## Notes")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        return "\n".join(lines)

    def get_progress(self) -> dict:
        """Get task completion progress."""
        total = len(self.tasks)
        completed = sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED)
        return {
            "total": total,
            "completed": completed,
            "percentage": (completed / total * 100) if total > 0 else 0
        }
```

### 2.3 Error Preservation Mechanism

**Key insight from Manus:** Keeping wrong turns in context helps models adapt and shows true agentic behavior.

```python
# In assistant_service.py - modify agentic loop

async def _handle_tool_error(
    self,
    tool_call: ToolCallRequest,
    error: Exception,
    messages: List[Dict],
    working_memory: WorkingMemory
) -> None:
    """
    Handle tool execution error - preserve in context for learning.
    DO NOT remove failed attempts from message history.
    """
    error_message = str(error)

    # Record in working memory
    working_memory.update_task(
        task_id=tool_call.id,
        status=TaskStatus.FAILED,
        error=error_message
    )
    working_memory.add_note(f"Tool {tool_call.name} failed: {error_message}")

    # Append error to messages (DO NOT pop the tool call)
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": f"Error: {error_message}"
    })

    # Add assistant acknowledgment to help model adapt
    messages.append({
        "role": "assistant",
        "content": f"The {tool_call.name} tool encountered an error: {error_message}. Let me try a different approach..."
    })
```

---

## 3. Task Planning & Tool Orchestration

### 3.1 Task Planner Component

```python
# src/services/assistant/task_planner.py

from dataclasses import dataclass
from typing import List, Set, Optional
from enum import Enum

class TaskType(str, Enum):
    RETRIEVE = "retrieve"      # KB search, web search
    GENERATE = "generate"      # Text, image, document
    ANALYZE = "analyze"        # Data analysis, comparison
    TRANSFORM = "transform"    # Format conversion, translation

@dataclass
class PlannedTask:
    id: str
    type: TaskType
    tool: str
    description: str
    parameters: dict
    dependencies: Set[str] = None  # Task IDs this depends on

    def __post_init__(self):
        if self.dependencies is None:
            self.dependencies = set()

@dataclass
class ExecutionPlan:
    goal: str
    tasks: List[PlannedTask]
    parallel_groups: List[List[str]]  # Groups of task IDs that can run in parallel

class TaskPlanner:
    """
    Analyzes user request and generates execution plan with dependency graph.
    """

    # Task patterns for common workflows
    WORKFLOW_PATTERNS = {
        "comparison": [
            {"type": TaskType.RETRIEVE, "count": 2, "parallel": True},
            {"type": TaskType.ANALYZE, "count": 1, "parallel": False},
            {"type": TaskType.GENERATE, "count": 1, "parallel": False},
        ],
        "report": [
            {"type": TaskType.RETRIEVE, "count": "n", "parallel": True},
            {"type": TaskType.ANALYZE, "count": 1, "parallel": False},
            {"type": TaskType.GENERATE, "count": 1, "parallel": False},
        ],
        "search_and_answer": [
            {"type": TaskType.RETRIEVE, "count": 1, "parallel": False},
            {"type": TaskType.GENERATE, "count": 1, "parallel": False},
        ],
    }

    async def create_plan(
        self,
        user_request: str,
        available_tools: List[str],
        context: Optional[str] = None
    ) -> ExecutionPlan:
        """
        Analyze request and create execution plan.
        Uses LLM to understand intent and decompose into tasks.
        """
        # This would call the LLM with a planning prompt
        # For now, return structure
        pass

    def analyze_dependencies(self, tasks: List[PlannedTask]) -> List[List[str]]:
        """
        Analyze task dependencies and group into parallel execution batches.
        Returns list of task ID groups that can execute in parallel.
        """
        # Build dependency graph
        remaining = {t.id: t for t in tasks}
        completed = set()
        parallel_groups = []

        while remaining:
            # Find tasks with all dependencies satisfied
            ready = [
                task_id for task_id, task in remaining.items()
                if task.dependencies.issubset(completed)
            ]

            if not ready:
                raise ValueError("Circular dependency detected in task plan")

            parallel_groups.append(ready)

            for task_id in ready:
                completed.add(task_id)
                del remaining[task_id]

        return parallel_groups
```

### 3.2 Parallel Tool Orchestrator

```python
# src/services/assistant/tool_orchestrator.py

import asyncio
from typing import List, Dict, Any, AsyncGenerator
from dataclasses import dataclass

@dataclass
class ToolExecutionResult:
    task_id: str
    tool: str
    success: bool
    result: Any = None
    error: str = None
    duration_ms: float = 0

class ToolOrchestrator:
    """
    Executes tools with parallel support and dependency coordination.
    """

    def __init__(self, tool_registry, max_parallel: int = 5):
        self.tool_registry = tool_registry
        self.max_parallel = max_parallel
        self.semaphore = asyncio.Semaphore(max_parallel)

    async def execute_plan(
        self,
        plan: 'ExecutionPlan',
        working_memory: 'WorkingMemory'
    ) -> AsyncGenerator[ToolExecutionResult, None]:
        """
        Execute plan respecting dependencies, yielding results as they complete.
        """
        completed_results: Dict[str, ToolExecutionResult] = {}

        for parallel_group in plan.parallel_groups:
            # Get tasks for this group
            tasks_to_run = [
                t for t in plan.tasks if t.id in parallel_group
            ]

            # Execute group in parallel
            async for result in self._execute_parallel(tasks_to_run, completed_results):
                completed_results[result.task_id] = result

                # Update working memory
                if result.success:
                    working_memory.update_task(
                        result.task_id,
                        TaskStatus.COMPLETED,
                        result=str(result.result)[:200]  # Truncate for memory
                    )
                else:
                    working_memory.update_task(
                        result.task_id,
                        TaskStatus.FAILED,
                        error=result.error
                    )

                yield result

    async def _execute_parallel(
        self,
        tasks: List['PlannedTask'],
        prior_results: Dict[str, ToolExecutionResult]
    ) -> AsyncGenerator[ToolExecutionResult, None]:
        """Execute a group of tasks in parallel."""

        async def run_one(task: 'PlannedTask') -> ToolExecutionResult:
            async with self.semaphore:
                start = asyncio.get_event_loop().time()
                try:
                    # Resolve any parameter references to prior results
                    params = self._resolve_params(task.parameters, prior_results)

                    # Execute tool
                    executor = self.tool_registry.get_executor(task.tool)
                    result = await executor.execute(params)

                    duration = (asyncio.get_event_loop().time() - start) * 1000
                    return ToolExecutionResult(
                        task_id=task.id,
                        tool=task.tool,
                        success=True,
                        result=result,
                        duration_ms=duration
                    )
                except Exception as e:
                    duration = (asyncio.get_event_loop().time() - start) * 1000
                    return ToolExecutionResult(
                        task_id=task.id,
                        tool=task.tool,
                        success=False,
                        error=str(e),
                        duration_ms=duration
                    )

        # Create all tasks
        coroutines = [run_one(task) for task in tasks]

        # Use as_completed to yield results as they finish
        for coro in asyncio.as_completed(coroutines):
            result = await coro
            yield result

    def _resolve_params(
        self,
        params: dict,
        prior_results: Dict[str, ToolExecutionResult]
    ) -> dict:
        """Resolve parameter references like ${task_1.result}"""
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str) and value.startswith("${"):
                # Parse reference: ${task_id.field}
                ref = value[2:-1]
                task_id, field = ref.split(".")
                if task_id in prior_results:
                    resolved[key] = getattr(prior_results[task_id], field)
                else:
                    resolved[key] = value
            else:
                resolved[key] = value
        return resolved
```

### 3.3 Write-While-Search Pattern

```python
# src/services/assistant/streaming_writer.py

from typing import AsyncGenerator, Optional
from dataclasses import dataclass

@dataclass
class StreamChunk:
    type: str  # "text", "search_start", "search_result", "search_end"
    content: str
    metadata: Optional[dict] = None

class StreamingWriter:
    """
    Enables write-while-search pattern: streaming text output
    with inline tool calls for fact-checking.
    """

    def __init__(self, kb_service, assistant_service):
        self.kb_service = kb_service
        self.assistant_service = assistant_service

    async def write_with_verification(
        self,
        writing_prompt: str,
        dataset_ids: List[str],
        verification_triggers: List[str] = None
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Generate text while verifying facts against knowledge base.

        Args:
            writing_prompt: The writing task
            dataset_ids: KB datasets to search
            verification_triggers: Phrases that trigger KB search
                                   (e.g., "according to", "policy states")
        """
        if verification_triggers is None:
            verification_triggers = [
                "according to", "policy states", "规定", "政策",
                "based on", "as per", "根据", "依据"
            ]

        # Buffer for detecting verification triggers
        buffer = ""

        async for chunk in self.assistant_service.stream_completion(writing_prompt):
            buffer += chunk

            # Check for verification triggers
            trigger_found = None
            for trigger in verification_triggers:
                if trigger.lower() in buffer.lower():
                    trigger_found = trigger
                    break

            if trigger_found:
                # Yield text up to trigger
                yield StreamChunk(type="text", content=buffer)
                buffer = ""

                # Extract query from context around trigger
                query = self._extract_verification_query(buffer, trigger_found)

                if query:
                    # Emit search start
                    yield StreamChunk(
                        type="search_start",
                        content=f"Verifying: {query}"
                    )

                    # Perform KB search
                    results = await self.kb_service.retrieve(
                        query=query,
                        dataset_ids=dataset_ids,
                        top_k=3
                    )

                    # Emit search results
                    yield StreamChunk(
                        type="search_result",
                        content=self._format_results(results),
                        metadata={"results": results}
                    )

                    yield StreamChunk(type="search_end", content="")

            # Yield buffered content periodically
            if len(buffer) > 100:
                yield StreamChunk(type="text", content=buffer)
                buffer = ""

        # Yield remaining buffer
        if buffer:
            yield StreamChunk(type="text", content=buffer)

    def _extract_verification_query(self, text: str, trigger: str) -> Optional[str]:
        """Extract the topic/query around a verification trigger."""
        # Simple extraction - in production, use NLP or LLM
        idx = text.lower().find(trigger.lower())
        if idx == -1:
            return None

        # Get surrounding context
        start = max(0, idx - 50)
        end = min(len(text), idx + len(trigger) + 100)
        context = text[start:end]

        return context.strip()

    def _format_results(self, results: List) -> str:
        """Format KB results for inline display."""
        if not results:
            return "No relevant information found."

        lines = []
        for i, r in enumerate(results[:3], 1):
            lines.append(f"[{i}] {r.content[:150]}...")
        return "\n".join(lines)
```

---

## 4. Memory Architecture

### 4.1 Three-Layer Memory Model

```python
# src/services/assistant/memory/memory_manager.py

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

class MemoryLayer(ABC):
    """Base class for memory layers."""

    @abstractmethod
    async def store(self, key: str, value: Any, metadata: dict = None) -> None:
        pass

    @abstractmethod
    async def retrieve(self, key: str) -> Optional[Any]:
        pass

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        pass

class WorkingMemoryLayer(MemoryLayer):
    """
    In-memory storage for current session task state.
    Fastest access, cleared on session end.
    """

    def __init__(self):
        self._store: Dict[str, Any] = {}
        self._metadata: Dict[str, dict] = {}

    async def store(self, key: str, value: Any, metadata: dict = None) -> None:
        self._store[key] = value
        self._metadata[key] = metadata or {}

    async def retrieve(self, key: str) -> Optional[Any]:
        return self._store.get(key)

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        # Simple keyword search for working memory
        results = []
        query_lower = query.lower()
        for key, value in self._store.items():
            if query_lower in str(value).lower():
                results.append({"key": key, "value": value})
                if len(results) >= limit:
                    break
        return results

    def clear(self) -> None:
        self._store.clear()
        self._metadata.clear()

class SessionMemoryLayer(MemoryLayer):
    """
    Database-backed storage for session data.
    Persists across reconnections, cleared on session delete.
    """

    def __init__(self, db, session_id: str):
        self.db = db
        self.session_id = session_id

    async def store(self, key: str, value: Any, metadata: dict = None) -> None:
        await self.db.store_session_memory(
            session_id=self.session_id,
            key=key,
            value=value,
            metadata=metadata
        )

    async def retrieve(self, key: str) -> Optional[Any]:
        return await self.db.get_session_memory(self.session_id, key)

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        return await self.db.search_session_memory(
            self.session_id, query, limit
        )

class LongTermMemoryLayer(MemoryLayer):
    """
    User-level persistent memory.
    Stores preferences, patterns, frequently used resources.
    """

    def __init__(self, db, user_id: str):
        self.db = db
        self.user_id = user_id

    async def store(self, key: str, value: Any, metadata: dict = None) -> None:
        await self.db.store_user_memory(
            user_id=self.user_id,
            key=key,
            value=value,
            metadata=metadata
        )

    async def retrieve(self, key: str) -> Optional[Any]:
        return await self.db.get_user_memory(self.user_id, key)

    async def search(self, query: str, limit: int = 10) -> List[Dict]:
        return await self.db.search_user_memory(self.user_id, query, limit)

    async def get_preferences(self) -> Dict[str, Any]:
        """Get user preferences."""
        prefs = await self.retrieve("preferences")
        return prefs or {
            "language": "zh-CN",
            "response_style": "professional",
            "preferred_tools": [],
            "default_datasets": [],
        }

    async def update_preferences(self, updates: Dict[str, Any]) -> None:
        """Update user preferences."""
        current = await self.get_preferences()
        current.update(updates)
        await self.store("preferences", current)

class MemoryManager:
    """
    Unified interface for three-layer memory system.
    """

    def __init__(self, db, user_id: str, session_id: str):
        self.working = WorkingMemoryLayer()
        self.session = SessionMemoryLayer(db, session_id)
        self.long_term = LongTermMemoryLayer(db, user_id)

    async def remember(
        self,
        key: str,
        value: Any,
        layer: str = "working",
        metadata: dict = None
    ) -> None:
        """Store in specified memory layer."""
        if layer == "working":
            await self.working.store(key, value, metadata)
        elif layer == "session":
            await self.session.store(key, value, metadata)
        elif layer == "long_term":
            await self.long_term.store(key, value, metadata)

    async def recall(self, key: str) -> Optional[Any]:
        """
        Recall from memory, checking layers in order:
        working -> session -> long_term
        """
        # Check working memory first (fastest)
        result = await self.working.retrieve(key)
        if result is not None:
            return result

        # Check session memory
        result = await self.session.retrieve(key)
        if result is not None:
            return result

        # Check long-term memory
        return await self.long_term.retrieve(key)

    async def search_all(self, query: str, limit: int = 10) -> List[Dict]:
        """Search across all memory layers."""
        results = []

        # Search each layer
        working_results = await self.working.search(query, limit)
        for r in working_results:
            r["layer"] = "working"
            results.append(r)

        session_results = await self.session.search(query, limit - len(results))
        for r in session_results:
            r["layer"] = "session"
            results.append(r)

        if len(results) < limit:
            lt_results = await self.long_term.search(query, limit - len(results))
            for r in lt_results:
                r["layer"] = "long_term"
                results.append(r)

        return results[:limit]
```

### 4.2 Intelligent Compression Strategy

```python
# src/services/assistant/memory/compressor.py

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import re

@dataclass
class CompressedContext:
    summary: str
    preserved_urls: List[str]
    preserved_code_blocks: List[str]
    key_artifacts: List[str]
    recent_messages: List[Dict[str, Any]]
    token_count: int

class ContextCompressor:
    """
    Intelligent context compression following Manus principles:
    - Preserve structure (URLs, code, tables) over prose
    - Maintain recoverability
    - Keep recent messages intact
    """

    # Patterns for content that should be preserved
    PRESERVE_PATTERNS = {
        "urls": r'https?://[^\s<>"{}|\\^`\[\]]+',
        "code_blocks": r'```[\s\S]*?```',
        "tables": r'\|[^\n]+\|[\n\r]+\|[-:| ]+\|[\s\S]*?(?=\n\n|\Z)',
        "json": r'\{[\s\S]*?\}',
    }

    def __init__(self, llm_service, max_summary_tokens: int = 500):
        self.llm_service = llm_service
        self.max_summary_tokens = max_summary_tokens

    async def compress(
        self,
        messages: List[Dict[str, Any]],
        target_tokens: int,
        preserve_recent: int = 6
    ) -> CompressedContext:
        """
        Compress conversation history to target token count.

        Args:
            messages: Full message history
            target_tokens: Target token count after compression
            preserve_recent: Number of recent messages to keep intact
        """
        # Always preserve recent messages
        recent = messages[-preserve_recent:] if len(messages) > preserve_recent else messages
        to_compress = messages[:-preserve_recent] if len(messages) > preserve_recent else []

        if not to_compress:
            return CompressedContext(
                summary="",
                preserved_urls=[],
                preserved_code_blocks=[],
                key_artifacts=[],
                recent_messages=recent,
                token_count=self._count_tokens(recent)
            )

        # Extract preservable content
        preserved_urls = self._extract_all(to_compress, "urls")
        preserved_code = self._extract_all(to_compress, "code_blocks")

        # Extract artifact references
        artifacts = self._extract_artifacts(to_compress)

        # Generate summary of compressed content
        summary = await self._generate_summary(to_compress)

        return CompressedContext(
            summary=summary,
            preserved_urls=list(set(preserved_urls))[:20],  # Limit URLs
            preserved_code_blocks=preserved_code[:5],  # Limit code blocks
            key_artifacts=artifacts,
            recent_messages=recent,
            token_count=self._estimate_compressed_tokens(
                summary, preserved_urls, preserved_code, recent
            )
        )

    def _extract_all(
        self,
        messages: List[Dict],
        pattern_name: str
    ) -> List[str]:
        """Extract all matches of a pattern from messages."""
        pattern = self.PRESERVE_PATTERNS.get(pattern_name)
        if not pattern:
            return []

        results = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = re.findall(pattern, content)
                results.extend(matches)

        return results

    def _extract_artifacts(self, messages: List[Dict]) -> List[str]:
        """Extract artifact IDs/names from messages."""
        artifacts = []
        artifact_pattern = r'artifact[_-]?(?:id)?[:\s]*([a-zA-Z0-9_-]+)'

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                matches = re.findall(artifact_pattern, content, re.IGNORECASE)
                artifacts.extend(matches)

        return list(set(artifacts))

    async def _generate_summary(self, messages: List[Dict]) -> str:
        """Generate concise summary of compressed messages."""
        # Combine message content
        content = "\n".join([
            f"{m['role']}: {m.get('content', '')}"
            for m in messages
        ])

        prompt = f"""Summarize this conversation history in 2-3 sentences.
Focus on: tasks attempted, results obtained, decisions made.

Conversation:
{content[:3000]}

Summary:"""

        summary = await self.llm_service.complete(prompt, max_tokens=200)
        return summary.strip()

    def _count_tokens(self, messages: List[Dict]) -> int:
        """Estimate token count for messages."""
        # Rough estimate: 4 chars per token
        total_chars = sum(
            len(str(m.get("content", ""))) for m in messages
        )
        return total_chars // 4

    def _estimate_compressed_tokens(
        self,
        summary: str,
        urls: List[str],
        code: List[str],
        recent: List[Dict]
    ) -> int:
        """Estimate total tokens after compression."""
        summary_tokens = len(summary) // 4
        url_tokens = sum(len(u) // 4 for u in urls)
        code_tokens = sum(len(c) // 4 for c in code)
        recent_tokens = self._count_tokens(recent)

        return summary_tokens + url_tokens + code_tokens + recent_tokens
```

---

## 5. Frontend UI Design

### 5.1 Task Panel Component

```typescript
// web/src/pages/assistant/components/TaskPanel.tsx

import React from 'react';
import { CheckCircle2, Circle, Loader2, AlertCircle, ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';

type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

interface Task {
  id: string;
  description: string;
  status: TaskStatus;
  result?: string;
  error?: string;
}

interface CollectedInfo {
  key: string;
  value: string;
  source: string;
}

interface TaskPanelProps {
  goal?: string;
  tasks: Task[];
  collectedInfo: CollectedInfo[];
  isVisible: boolean;
}

const statusIcons: Record<TaskStatus, React.ReactNode> = {
  pending: <Circle className="h-4 w-4 text-muted-foreground" />,
  in_progress: <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />,
  completed: <CheckCircle2 className="h-4 w-4 text-green-500" />,
  failed: <AlertCircle className="h-4 w-4 text-red-500" />,
};

export function TaskPanel({ goal, tasks, collectedInfo, isVisible }: TaskPanelProps) {
  if (!isVisible || tasks.length === 0) return null;

  const completedCount = tasks.filter(t => t.status === 'completed').length;
  const progress = tasks.length > 0 ? (completedCount / tasks.length) * 100 : 0;

  return (
    <div className="w-80 border-l bg-muted/30 p-4 space-y-4 overflow-y-auto">
      {/* Header with progress */}
      <div className="space-y-2">
        <h3 className="font-semibold text-sm">Task Progress</h3>
        <div className="flex items-center gap-2">
          <div className="flex-1 h-2 bg-muted rounded-full overflow-hidden">
            <div
              className="h-full bg-primary transition-all duration-300"
              style={{ width: `${progress}%` }}
            />
          </div>
          <span className="text-xs text-muted-foreground">
            {completedCount}/{tasks.length}
          </span>
        </div>
      </div>

      {/* Goal */}
      {goal && (
        <div className="p-3 bg-background rounded-lg border">
          <div className="text-xs text-muted-foreground mb-1">Goal</div>
          <div className="text-sm">{goal}</div>
        </div>
      )}

      {/* Task List */}
      <div className="space-y-2">
        <h4 className="text-xs font-medium text-muted-foreground uppercase">Tasks</h4>
        {tasks.map((task) => (
          <div
            key={task.id}
            className={cn(
              "flex items-start gap-2 p-2 rounded-md text-sm",
              task.status === 'in_progress' && "bg-blue-500/10",
              task.status === 'failed' && "bg-red-500/10"
            )}
          >
            {statusIcons[task.status]}
            <div className="flex-1 min-w-0">
              <div className={cn(
                task.status === 'completed' && "line-through text-muted-foreground"
              )}>
                {task.description}
              </div>
              {task.error && (
                <div className="text-xs text-red-500 mt-1">{task.error}</div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Collected Information */}
      {collectedInfo.length > 0 && (
        <Collapsible defaultOpen>
          <CollapsibleTrigger className="flex items-center gap-2 text-xs font-medium text-muted-foreground uppercase w-full">
            <ChevronDown className="h-3 w-3" />
            Collected Information ({collectedInfo.length})
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2 space-y-2">
            {collectedInfo.map((info, idx) => (
              <div key={idx} className="p-2 bg-background rounded border text-xs">
                <div className="font-medium">{info.key}</div>
                <div className="text-muted-foreground mt-1 line-clamp-2">
                  {info.value}
                </div>
                <div className="text-muted-foreground/60 mt-1">
                  via {info.source}
                </div>
              </div>
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}
```

### 5.2 Parallel Execution Visualization

```typescript
// web/src/pages/assistant/components/ParallelExecutionView.tsx

import React from 'react';
import { Loader2, CheckCircle2, XCircle, Clock } from 'lucide-react';
import { Progress } from '@/components/ui/progress';

interface ToolExecution {
  id: string;
  tool: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  progress?: number;
  duration?: number;
  result?: string;
  error?: string;
}

interface ParallelGroup {
  groupId: number;
  executions: ToolExecution[];
}

interface ParallelExecutionViewProps {
  groups: ParallelGroup[];
  currentGroup: number;
}

const toolIcons: Record<string, string> = {
  'kb_search': 'Search',
  'web_search': 'Globe',
  'code_executor': 'Code',
  'image_generator': 'Image',
  'document_generator': 'FileText',
};

export function ParallelExecutionView({ groups, currentGroup }: ParallelExecutionViewProps) {
  return (
    <div className="space-y-3 p-3 bg-muted/30 rounded-lg">
      <div className="text-xs font-medium text-muted-foreground">
        Tool Execution
      </div>

      {groups.map((group, groupIdx) => (
        <div key={group.groupId} className="space-y-2">
          {/* Group header */}
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>Step {groupIdx + 1}</span>
            {group.executions.length > 1 && (
              <span className="px-1.5 py-0.5 bg-blue-500/20 text-blue-600 rounded">
                Parallel x{group.executions.length}
              </span>
            )}
          </div>

          {/* Executions in this group */}
          <div className="grid gap-2" style={{
            gridTemplateColumns: `repeat(${Math.min(group.executions.length, 3)}, 1fr)`
          }}>
            {group.executions.map((exec) => (
              <ToolExecutionCard key={exec.id} execution={exec} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ToolExecutionCard({ execution }: { execution: ToolExecution }) {
  const { status, tool, progress, duration, error } = execution;

  return (
    <div className={cn(
      "p-2 rounded border text-xs",
      status === 'running' && "border-blue-500 bg-blue-500/5",
      status === 'completed' && "border-green-500/50 bg-green-500/5",
      status === 'failed' && "border-red-500/50 bg-red-500/5"
    )}>
      <div className="flex items-center justify-between mb-1">
        <span className="font-medium">{tool}</span>
        {status === 'running' && <Loader2 className="h-3 w-3 animate-spin text-blue-500" />}
        {status === 'completed' && <CheckCircle2 className="h-3 w-3 text-green-500" />}
        {status === 'failed' && <XCircle className="h-3 w-3 text-red-500" />}
        {status === 'pending' && <Clock className="h-3 w-3 text-muted-foreground" />}
      </div>

      {status === 'running' && progress !== undefined && (
        <Progress value={progress} className="h-1 mt-1" />
      )}

      {duration !== undefined && (
        <div className="text-muted-foreground mt-1">
          {duration}ms
        </div>
      )}

      {error && (
        <div className="text-red-500 mt-1 truncate" title={error}>
          {error}
        </div>
      )}
    </div>
  );
}
```

### 5.3 Enhanced Artifact Workspace

```typescript
// web/src/pages/assistant/components/ArtifactWorkspace.tsx

import React, { useState } from 'react';
import {
  FileText, Image, Code, Download, Copy, History,
  Maximize2, X, RefreshCw
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

interface ArtifactVersion {
  id: string;
  version: number;
  createdAt: Date;
  preview?: string;
}

interface Artifact {
  id: string;
  type: 'document' | 'code' | 'image' | 'data';
  name: string;
  content: string;
  mimeType: string;
  versions: ArtifactVersion[];
  currentVersion: number;
}

interface ArtifactWorkspaceProps {
  artifacts: Artifact[];
  activeArtifactId?: string;
  onArtifactSelect: (id: string) => void;
  onDownload: (artifact: Artifact) => void;
  onCopy: (artifact: Artifact) => void;
  onRefine: (artifact: Artifact) => void;
  isExpanded: boolean;
  onToggleExpand: () => void;
}

const typeIcons = {
  document: FileText,
  code: Code,
  image: Image,
  data: FileText,
};

export function ArtifactWorkspace({
  artifacts,
  activeArtifactId,
  onArtifactSelect,
  onDownload,
  onCopy,
  onRefine,
  isExpanded,
  onToggleExpand,
}: ArtifactWorkspaceProps) {
  const activeArtifact = artifacts.find(a => a.id === activeArtifactId);

  if (artifacts.length === 0) {
    return null;
  }

  return (
    <div className={cn(
      "border-l bg-background flex flex-col",
      isExpanded ? "w-[600px]" : "w-80"
    )}>
      {/* Header */}
      <div className="flex items-center justify-between p-3 border-b">
        <h3 className="font-semibold text-sm">Artifacts</h3>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" onClick={onToggleExpand}>
            <Maximize2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Artifact tabs */}
      <Tabs value={activeArtifactId} onValueChange={onArtifactSelect} className="flex-1 flex flex-col">
        <TabsList className="justify-start px-2 py-1 h-auto flex-wrap">
          {artifacts.map((artifact) => {
            const Icon = typeIcons[artifact.type];
            return (
              <TabsTrigger
                key={artifact.id}
                value={artifact.id}
                className="gap-1 text-xs"
              >
                <Icon className="h-3 w-3" />
                {artifact.name}
              </TabsTrigger>
            );
          })}
        </TabsList>

        {artifacts.map((artifact) => (
          <TabsContent
            key={artifact.id}
            value={artifact.id}
            className="flex-1 flex flex-col m-0"
          >
            {/* Artifact toolbar */}
            <div className="flex items-center gap-1 p-2 border-b">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onDownload(artifact)}
              >
                <Download className="h-3 w-3 mr-1" />
                Download
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onCopy(artifact)}
              >
                <Copy className="h-3 w-3 mr-1" />
                Copy
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onRefine(artifact)}
              >
                <RefreshCw className="h-3 w-3 mr-1" />
                Refine
              </Button>

              {/* Version history */}
              {artifact.versions.length > 1 && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="sm">
                      <History className="h-3 w-3 mr-1" />
                      v{artifact.currentVersion}
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent>
                    {artifact.versions.map((v) => (
                      <DropdownMenuItem key={v.id}>
                        Version {v.version} - {v.createdAt.toLocaleTimeString()}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>

            {/* Preview area */}
            <ScrollArea className="flex-1">
              <ArtifactPreview artifact={artifact} />
            </ScrollArea>
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function ArtifactPreview({ artifact }: { artifact: Artifact }) {
  switch (artifact.type) {
    case 'image':
      return (
        <div className="p-4 flex items-center justify-center">
          <img
            src={`data:${artifact.mimeType};base64,${artifact.content}`}
            alt={artifact.name}
            className="max-w-full max-h-[400px] object-contain rounded"
          />
        </div>
      );

    case 'code':
      return (
        <pre className="p-4 text-xs font-mono overflow-x-auto">
          <code>{artifact.content}</code>
        </pre>
      );

    case 'document':
      return (
        <div className="p-4 prose prose-sm max-w-none">
          {/* Render markdown or plain text */}
          {artifact.content}
        </div>
      );

    default:
      return (
        <div className="p-4 text-sm text-muted-foreground">
          Preview not available for this file type.
        </div>
      );
  }
}
```

### 5.4 New SSE Event Types

```typescript
// web/src/pages/assistant/types.ts - additions

// Task planning events
export interface TaskPlanningEvent {
  type: 'task_planning';
  goal: string;
  tasks: PlannedTask[];
}

export interface PlannedTask {
  id: string;
  description: string;
  tool: string;
  dependencies: string[];
}

// Parallel execution events
export interface ParallelExecutionStartEvent {
  type: 'parallel_execution_start';
  groupId: number;
  tools: string[];
}

export interface ToolProgressEvent {
  type: 'tool_progress';
  taskId: string;
  tool: string;
  progress: number;  // 0-100
}

export interface ToolCompleteEvent {
  type: 'tool_complete';
  taskId: string;
  tool: string;
  success: boolean;
  duration_ms: number;
  result?: any;
  error?: string;
}

// Working memory events
export interface WorkingMemoryUpdateEvent {
  type: 'working_memory_update';
  taskState: string;  // todo.md format
  collectedInfo: CollectedInfo[];
}

export interface CollectedInfo {
  key: string;
  value: string;
  source: string;
}

// Extend AssistantStreamEvent union
export type AssistantStreamEvent =
  | TextDeltaEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ContextRetrievedEvent
  | WebSearchResultsEvent
  | CodeExecutionStartEvent
  | CodeExecutionResultEvent
  | ImageGenerationStartEvent
  | ImageGenerationResultEvent
  | DocumentGenerationStartEvent
  | DocumentGenerationResultEvent
  | CacheMetricsEvent
  | RagEvaluationEvent
  | UsageEvent
  | DoneEvent
  | ErrorEvent
  // New events
  | TaskPlanningEvent
  | ParallelExecutionStartEvent
  | ToolProgressEvent
  | ToolCompleteEvent
  | WorkingMemoryUpdateEvent;
```

---

## 6. Database Schema Additions

```sql
-- database/migrations/022_assistant_memory.sql

-- Session-level memory storage
CREATE TABLE IF NOT EXISTS session_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES assistant_sessions(id) ON DELETE CASCADE,
    key VARCHAR(255) NOT NULL,
    value JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, key)
);

CREATE INDEX idx_session_memory_session ON session_memory(session_id);
CREATE INDEX idx_session_memory_key ON session_memory(key);

-- User-level long-term memory
CREATE TABLE IF NOT EXISTS user_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL,
    key VARCHAR(255) NOT NULL,
    value JSONB NOT NULL,
    metadata JSONB DEFAULT '{}',
    access_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, user_id, key)
);

CREATE INDEX idx_user_memory_user ON user_memory(tenant_id, user_id);
CREATE INDEX idx_user_memory_key ON user_memory(key);
CREATE INDEX idx_user_memory_access ON user_memory(last_accessed_at DESC);

-- Task state tracking
CREATE TABLE IF NOT EXISTS task_states (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES assistant_sessions(id) ON DELETE CASCADE,
    message_id UUID,  -- Associated message that triggered the task
    goal TEXT,
    status VARCHAR(50) NOT NULL DEFAULT 'planning',  -- planning, executing, completed, failed
    tasks JSONB NOT NULL DEFAULT '[]',  -- Array of task items
    collected_info JSONB NOT NULL DEFAULT '[]',  -- Array of collected information
    notes JSONB NOT NULL DEFAULT '[]',  -- Array of notes
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_task_states_session ON task_states(session_id);
CREATE INDEX idx_task_states_status ON task_states(status);

-- Artifact versions
CREATE TABLE IF NOT EXISTS artifact_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    storage_path TEXT NOT NULL,
    changes_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(artifact_id, version)
);

CREATE INDEX idx_artifact_versions_artifact ON artifact_versions(artifact_id);

-- Add version tracking to artifacts table
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS current_version INTEGER DEFAULT 1;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS version_count INTEGER DEFAULT 1;

-- Triggers for updated_at
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER session_memory_updated
    BEFORE UPDATE ON session_memory
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER user_memory_updated
    BEFORE UPDATE ON user_memory
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER task_states_updated
    BEFORE UPDATE ON task_states
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();
```

---

## 7. Implementation Phases

### Phase 1: Context Engine Foundation

**Goal:** Improve response speed and task coherence

| Task | Files | Description |
|------|-------|-------------|
| 1.1 | `src/services/assistant/context_engine.py` | Create ContextEngine class with stable prefix design |
| 1.2 | `src/services/assistant/working_memory.py` | Implement WorkingMemory with todo.md format |
| 1.3 | `src/services/assistant/assistant_service.py` | Integrate ContextEngine into chat_stream |
| 1.4 | `src/services/assistant/assistant_service.py` | Modify agentic loop for error preservation |
| 1.5 | Tests | Unit tests for Context Engine and Working Memory |

### Phase 2: Task Planning & Parallel Execution

**Goal:** Support complex multi-step tasks

| Task | Files | Description |
|------|-------|-------------|
| 2.1 | `src/services/assistant/task_planner.py` | Create TaskPlanner with dependency analysis |
| 2.2 | `src/services/assistant/tool_orchestrator.py` | Implement parallel tool execution |
| 2.3 | `src/services/assistant/streaming_writer.py` | Add write-while-search capability |
| 2.4 | `src/services/assistant/assistant_service.py` | Integrate planner and orchestrator |
| 2.5 | Tests | Integration tests for parallel execution |

### Phase 3: Memory Architecture

**Goal:** Enable cross-session intelligence

| Task | Files | Description |
|------|-------|-------------|
| 3.1 | `database/migrations/022_assistant_memory.sql` | Create memory tables |
| 3.2 | `src/services/assistant/memory/memory_manager.py` | Implement three-layer memory |
| 3.3 | `src/services/assistant/memory/compressor.py` | Add intelligent compression |
| 3.4 | `src/persistence/database.py` | Add memory CRUD operations |
| 3.5 | `src/services/assistant/assistant_service.py` | Integrate memory manager |

### Phase 4: Frontend Enhancement

**Goal:** Improve visibility and user experience

| Task | Files | Description |
|------|-------|-------------|
| 4.1 | `web/src/pages/assistant/components/TaskPanel.tsx` | Create task tracking panel |
| 4.2 | `web/src/pages/assistant/components/ParallelExecutionView.tsx` | Add execution visualization |
| 4.3 | `web/src/pages/assistant/components/ArtifactWorkspace.tsx` | Enhance artifact workspace |
| 4.4 | `web/src/pages/assistant/types.ts` | Add new event types |
| 4.5 | `web/src/pages/assistant/index.tsx` | Integrate new components |

---

## 8. Success Metrics

| Metric | Current | Target | Measurement |
|--------|---------|--------|-------------|
| KV-Cache Hit Rate | ~0% | >70% | Provider metrics |
| Multi-step Task Completion | N/A | >80% | Task state tracking |
| Average Response Latency | ~3s | <2s | P50 latency |
| User Task Success Rate | Unknown | >85% | Explicit feedback |
| Context Utilization | Unknown | >60% | RAG metrics |

---

## 9. References

- [Manus Context Engineering Blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)
- [Salesforce Agentic Enterprise Architecture](https://architect.salesforce.com/fundamentals/agentic-enterprise-it-architecture)
- [Kore.ai Agentic Architecture Blueprint](https://www.kore.ai/blog/agentic-architecture-blueprint-for-intelligent-enterprise)
- [BCG: How Agentic AI is Transforming Enterprise Platforms](https://www.bcg.com/publications/2025/how-agentic-ai-is-transforming-enterprise-platforms)
