"""
Assistant Service - GPT-like chat experience with KB integration.

Provides:
- Multi-model support via ModelRegistry
- Automatic KB retrieval (RAG)
- Web search via Tavily API
- Streaming responses with tool call visualization
- Session persistence with user isolation
- Intelligent context management (sliding window + token-aware truncation)

Context Management Strategy (based on industry best practices):
- Sliding window: Keep last 30 messages by default
- Token-aware truncation: Respect model context limits (use 85% capacity)
- Always preserve at least 6 recent messages
- Critical data in early positions for better recall

References:
- https://mem0.ai/blog/llm-chat-history-summarization-guide-2025
- https://www.getmaxim.ai/articles/context-window-management-strategies
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

if TYPE_CHECKING:
    from ai_gateway_core.knowledge import KnowledgeClientLike
    from ai_gateway_core.session import SessionManagerLike
    from .code_executor import InputFile, KBDocument
    from .memory_service import MemoryService

from cachetools import TTLCache
from ai_gateway_core.auth import UserContext
from ai_gateway_core.exceptions import PermissionDeniedError
from ai_gateway_core.metrics import (
    NoOpRealtimeMetrics,
    NoOpUsageRecorder,
    RealtimeMetricsLike,
    UsageRecorderLike,
)
from ai_gateway_core.storage import (
    ArtifactStorageLike,
    FileStorageLike,
    NoOpArtifactStorage,
    NoOpFileStorage,
)
# Module-level NoOp singletons used as DI defaults. These are safe to share —
# they hold no mutable state and every method is a silent no-op.
_DEFAULT_NOOP_USAGE_RECORDER: UsageRecorderLike = NoOpUsageRecorder()
_DEFAULT_NOOP_REALTIME_METRICS: RealtimeMetricsLike = NoOpRealtimeMetrics()
_DEFAULT_NOOP_ARTIFACT_STORAGE: ArtifactStorageLike = NoOpArtifactStorage()
_DEFAULT_NOOP_FILE_STORAGE: FileStorageLike = NoOpFileStorage()
from .agent.agent_loop import PRIOR_TOOL_RESULTS_MARKER, AgentLoopEvent
from .quality.cache_optimizer import CacheConfig, ContextCacheOptimizer
from .code_executor import CodeExecutorService
from .rag.context_engine import ContextEngine, ContextStructure
from .rag.context_manager import ContextConfig, get_context_manager
from .quality.domain_policies import DomainPolicyResolver, ImamPolicy
from .files.file_processor import ProcessedFiles, create_file_processor
from .quality.guardrails import (
    DocumentType,
    QualityGuardrails,
    ToolCallValidation,
    ToolConstraintValidator,
    ValidationResult,
)
from .memory import MemoryManager
from .models.model_registry import ChatMessage, ModelProvider, ModelRegistry
from .prompts.system_prompt_v2 import (
    build_system_prompt_v2,
    get_ttft_optimized_prompt,
    inject_document_context,
    inject_kb_context,
    inject_user_preferences,
    inject_web_context,
)
from .rag.rag_metrics import (
    Citation,
    RAGMetrics,
    get_rag_evaluator,
)
from .agent.react_executor import ReActPhase
from .rag.scenario_analyzer import (
    ScenarioDetectionResult,
    create_scenario_analyzer,
)
from .content.structured_output import (
    OutputFormat,
    OutputGuardrail,
)
from .tasks.task_planner import TaskPlanner, create_task_planner
from .tool_invoker import ToolInvocationContext
from .tool_orchestrator import ToolExecutionResult, ToolOrchestrator, create_tool_orchestrator
from .tools import TavilySearchTool
from .tools.code_executor_tool import CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor
from .working_memory import WorkingMemory

logger = get_logger(__name__)


class RAGMode(str, Enum):
    """RAG behavior mode."""

    AUTO = "auto"  # Auto-retrieve on each message
    TOOL = "tool"  # KB exposed as callable tool
    DISABLED = "off"  # No KB retrieval


class StreamEventType(str, Enum):
    """SSE event types for assistant streaming responses."""

    # Core streaming events
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_START = "thinking_start"
    THINKING_END = "thinking_end"
    THINKING_ERROR = "thinking_error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_CALL_RESULT = "tool_call_result"  # AG-UI compatible tool result
    TOOL_CALL_START = "tool_call_start"  # AG-UI tool call lifecycle start
    TOOL_CALL_END = "tool_call_end"  # AG-UI tool call lifecycle end

    # Context and retrieval events
    CONTEXT_RETRIEVED = "context_retrieved"
    WEB_SEARCH_RESULTS = "web_search_results"
    RAG_EVALUATION = "rag_evaluation"
    CONTEXT_BUDGET = "context_budget"
    CONTEXT_COMPACTED = "context_compacted"
    CONTEXT_DETAIL = "context_detail"
    MEMORY_RETRIEVED = "memory_retrieved"
    MEMORY_REFLECTION_SCHEDULED = "memory_reflection_scheduled"
    QUEUE_STEERED = "queue_steered"
    SKILL_SELECTED = "skill_selected"
    SKILL_LOADED = "skill_loaded"
    SKILL_CREATE_PENDING_APPROVAL = "skill_create_pending_approval"
    SANDBOX_DECISION = "sandbox_decision"

    # Gateway / queue / approvals
    QUEUE_STATE = "queue_state"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESULT = "approval_result"
    GATEWAY_DECISION = "gateway_decision"

    # Session events
    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"

    # Status events
    STATUS = "status"
    USAGE = "usage"
    FINISH = "finish"
    DONE = "done"
    ERROR = "error"
    OUTPUT_WARNINGS = "output_warnings"

    # Code execution events
    CODE_EXECUTION_START = "code_execution_start"
    CODE_EXECUTION_OUTPUT = "code_execution_output"
    CODE_EXECUTION_RESULT = "code_execution_result"
    ARTIFACT_CREATED = "artifact_created"

    # Image generation events
    IMAGE_GENERATION_START = "image_generation_start"
    IMAGE_GENERATION_RESULT = "image_generation_result"

    # Document generation events
    DOCUMENT_GENERATION_START = "document_generation_start"
    DOCUMENT_GENERATION_RESULT = "document_generation_result"

    # KV-Cache metrics
    CACHE_METRICS = "cache_metrics"

    # File processing events
    FILE_PROCESSED = "file_processed"

    # Working memory events (Context Engine)
    WORKING_MEMORY_UPDATE = "working_memory_update"
    TASK_PLANNING = "task_planning"

    # Memory manager events
    MEMORY_LOADED = "memory_loaded"

    # Tool execution error event (for error preservation)
    TOOL_ERROR = "tool_error"

    # Manus-style task visualization events
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    OUTLINE_READY = "outline_ready"

    # Run lifecycle events
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_ERROR = "run_error"


@dataclass
class AssistantConfig:
    """Configuration for an assistant conversation."""

    # Model settings
    model_provider: ModelProvider = ModelProvider.DASHSCOPE
    model_id: str = "qwen3.6-plus"
    temperature: float = 0.7
    max_tokens: int | None = None

    # Knowledge base settings (TTFT-optimized defaults)
    kb_dataset_ids: list[str] = field(default_factory=list)
    kb_mode: RAGMode = RAGMode.AUTO
    kb_top_k: int = 5  # Number of KB results to retrieve
    kb_score_threshold: float = 0.65  # Increased from 0.5 for higher quality results
    kb_include_images: bool = False
    kb_max_content_length: int = 400  # Max chars per chunk to reduce context size

    # Web search settings
    web_search_enabled: bool = False
    web_search_max_results: int = 5

    # File attachments
    file_paths: list[str] = field(default_factory=list)

    # System prompt
    system_prompt: str | None = None

    # Tools (future extension)
    tools_enabled: list[str] = field(default_factory=list)

    # Phase 4: Output validation settings
    output_max_length: int = 10000
    output_check_pii: bool = True
    output_format: OutputFormat = OutputFormat.TEXT

    # Context Engine settings (Phase 5: KV-Cache optimization)
    use_context_engine: bool = True  # ENABLED: Use Context Engine for KV-Cache optimization
    user_preferences: str | None = None  # User-level preferences for context
    long_term_memory: str | None = None  # Persistent user knowledge

    # Task Planning settings (Phase 2.4: Multi-step task planning)
    enable_task_planning: bool = (
        False  # Disabled by default - enable for complex multi-step tasks only
    )
    confirm_plan: bool = (
        False  # When True, pause and require user confirmation before executing template plans
    )
    max_parallel_tools: int = 5  # Maximum number of tools to execute in parallel

    # Agent Loop settings (Enterprise unified 8-step flow)
    use_agent_loop: bool = True  # Enabled for streaming-first TTFT optimization
    use_scenario_retrieval: bool = (
        False  # Disabled - scenario detection still runs but no heavy retrieval
    )
    enable_rag_metrics: bool = False  # Disabled in production for performance

    # Memory and ReAct settings (Manus architecture core features)
    enable_memory_loading: bool = False  # Disabled by default - reduces TTFT significantly
    enable_react_loop: bool = False  # Disabled by default - simple generation for most queries

    # Assistant Gateway policy profile (OpenClaw-style)
    execution_profile: str = "safe"  # safe | balanced | power
    memory_mode: str = "auto"  # auto | strict | off
    os_agent_enabled: bool = False  # gated by policy engine + tenant/user permissions
    openclaw_mode: str = "compat"  # off | compat | full
    queue_mode: str = "collect"  # collect | followup | steer | interrupt
    context_detail: bool = False  # emit detailed context cost breakdown
    skills_enabled: bool | None = None  # per-request skill toggle
    memory_profile: str | None = None  # off | basic | hybrid


@dataclass
class AssistantStreamEvent:
    """Event emitted during streaming."""

    event_type: str  # context_retrieved, text_delta, tool_call, tool_result, usage, done
    data: Any = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievedContext:
    """Context retrieved from knowledge bases."""

    dataset_id: str
    dataset_name: str
    chunks: list[dict[str, Any]]
    query: str
    took_ms: float

    # Phase 3: RAG metrics
    avg_score: float = 0.0
    top_score: float = 0.0


@dataclass
class RAGEvaluation:
    """Phase 3: RAG evaluation results for a conversation turn."""

    metrics: RAGMetrics | None = None
    citations: list[Citation] = field(default_factory=list)
    quality_score: float = 0.0
    grounding_ratio: float = 0.0  # What % of response is grounded in sources


@dataclass
class ToolErrorInfo:
    """
    Structured error information for tool execution failures.

    Based on Manus Context Engineering principle: Don't hide failures from the agent.
    By preserving rich error context, the model can:
    - Understand what went wrong
    - Adjust its approach on retry
    - Provide better feedback to users

    Attributes:
        tool_name: Name of the tool that failed
        tool_call_id: ID of the tool call
        error_type: Type/class of the error
        error_message: Human-readable error message
        arguments: The arguments that were passed to the tool
        suggestion: Optional suggestion for how to fix the issue
        timestamp: When the error occurred
    """

    tool_name: str
    tool_call_id: str
    error_type: str
    error_message: str
    arguments: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_rich_context(self) -> str:
        """
        Format error as rich context for the model.

        This format is designed to help the model understand and potentially
        recover from the error. The structure is:
        - Clear error type and message
        - Arguments that caused the failure
        - Actionable suggestion when available
        """
        lines = [
            f"[TOOL ERROR] {self.tool_name} failed",
            f"Error Type: {self.error_type}",
            f"Error Message: {self.error_message}",
        ]

        if self.arguments:
            args_str = ", ".join(f"{k}={repr(v)[:100]}" for k, v in self.arguments.items())
            lines.append(f"Arguments: {args_str}")

        if self.suggestion:
            lines.append(f"Suggestion: {self.suggestion}")

        return "\n".join(lines)


def _session_history_to_messages(
    session_history: list[Any],
) -> list[dict[str, Any]]:
    """Convert ``SessionMessage`` records to the ``{role, content}`` shape the
    rest of the pipeline expects, while preserving prior tool output so
    cross-turn (and especially cross-model) follow-ups can reference it.

    Bug fix 2026-04-21: previously this was a naive
    ``[{"role": m.role, "content": m.content} for m in session.history]``.
    That drops ``m.metadata.tool_results`` entirely — so when a tool (quiz,
    web search, …) produced rich output on turn N and the user asked
    "explain those questions above" on turn N+1 (possibly on a different
    model that doesn't keep provider-side state), the follow-up model saw
    only the assistant's terse text ("已为您生成5道测试题…") and
    hallucinated a brand-new quiz.

    We now append a compact ``[Previous tool results]`` block to the
    assistant message content whenever the persisted metadata contains
    tool_results. The block is bounded per-tool and per-turn to keep
    history budgets predictable.
    """
    out: list[dict[str, Any]] = []
    for m in session_history or []:
        role = getattr(m, "role", None) or "user"
        content = getattr(m, "content", "")
        if role not in ("user", "assistant"):
            continue
        metadata = getattr(m, "metadata", None) or {}
        tool_results = metadata.get("tool_results") if isinstance(metadata, dict) else None
        if role == "assistant" and isinstance(tool_results, list) and tool_results:
            content = _append_tool_results_block(str(content or ""), tool_results)
        out.append({"role": role, "content": content})
    return out


def _append_tool_results_block(
    content: str,
    tool_results: list[dict[str, Any]],
    *,
    per_tool_char_cap: int = 2000,
    total_char_cap: int = 6000,
) -> str:
    """Append a ``[Previous tool results]`` block to an assistant message.

    Each tool entry is capped independently so one verbose tool (e.g. a
    50-question quiz) can't crowd out the others; a total budget also
    protects the full history from blowing up token counts. The block is
    framed with explicit markers so the next-turn model reads it as prior
    context rather than a new instruction.
    """
    if not tool_results:
        return content

    total_used = 0
    # NOTE: the opening line MUST start with PRIOR_TOOL_RESULTS_MARKER —
    # agent_loop._trim_history_for_streaming matches on that prefix to
    # enlarge the per-message cap for messages carrying this block.
    lines: list[str] = [
        "",
        f"{PRIOR_TOOL_RESULTS_MARKER} — for your reference only, "
        f"not shown to the user]",
    ]
    for entry in tool_results:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "tool").strip() or "tool"
        result = entry.get("result")
        if result is None:
            continue
        text = str(result).strip()
        if not text:
            continue
        if len(text) > per_tool_char_cap:
            text = text[: per_tool_char_cap - 3].rstrip() + "..."
        remaining = total_char_cap - total_used
        if remaining <= 0:
            lines.append("... [more tool results truncated from history]")
            break
        if len(text) > remaining:
            text = text[: max(0, remaining - 3)].rstrip() + "..."
        lines.append(f"- {name}: {text}")
        total_used += len(text)
    lines.append("[End previous tool results]")
    return (content or "") + "\n" + "\n".join(lines)


class AssistantService:
    """
    GPT-like assistant with multi-model support and KB integration.

    Usage:
        assistant = AssistantService(model_registry, kb_service)

        config = AssistantConfig(
            model_id="qwen3.6-plus",
            kb_dataset_ids=["docs", "wiki"],
        )

        async for event in assistant.chat_stream(user, session_id, "What is our refund policy?", config, history):
            if event.event_type == "text_delta":
                print(event.data, end="")
            elif event.event_type == "context_retrieved":
                print(f"Found {len(event.data.chunks)} relevant chunks")
    """

    # Default system prompt when none provided (Legacy - kept for backwards compatibility)
    DEFAULT_SYSTEM_PROMPT_LEGACY = """You are a helpful AI assistant with access to tools via function calling.

## How to Use Tools

You have access to tools like `mcp_docgen__generate_document`, `web_search`, etc.

**CRITICAL**: To use a tool, you must trigger the function calling mechanism - NOT write tool calls as text in your response.
- WRONG: Writing `mcp_docgen__generate_document(format="pptx", ...)` in your text response
- RIGHT: Using the function calling feature to invoke the tool

When you need to use a tool:
1. Briefly explain what you will do
2. Then invoke the tool via function calling (the system handles this automatically)
3. After the tool executes, summarize the result

## Task Guidelines

### Answering Questions
- Use provided context when available
- Be accurate and cite sources

### Document Generation (Word / PowerPoint / Excel / PDF)
One tool covers all four formats: ``mcp_docgen__generate_document``.
- ``format``: "docx" | "pptx" | "xlsx" | "pdf"
- ``title``: document title
- ``goal``: one sentence on audience + intent (drives the planner)
- ``body_markdown``: optional markdown ground-truth content

Workflow:
1. Briefly outline the structure (1-2 sentences)
2. Invoke the tool with the appropriate ``format``
3. Tell the user when the file is ready — it will surface as a download link

### General Rules
- Tell the user when files are ready
- Do NOT output JSON or function call syntax in your text"""

    # Manus-style modular system prompt (v2) - built dynamically
    @classmethod
    def build_default_system_prompt(
        cls,
        user_role: str = "user",
        available_datasets: list[str] | None = None,
        enabled_tools: list[str] | None = None,
        scenario_rules: str = "",
    ) -> str:
        """
        Build the default Manus-style system prompt.

        This is the new recommended approach that uses modular prompt design
        with clear separation of guardrails and agent freedom.

        Args:
            user_role: User's role for access display
            available_datasets: List of available KB names
            enabled_tools: List of enabled tools
            scenario_rules: Scenario-specific rules

        Returns:
            Complete Manus-style system prompt
        """
        base_prompt = build_system_prompt_v2(
            user_role=user_role,
            available_datasets=available_datasets,
            enabled_tools=enabled_tools,
            scenario_rules=scenario_rules,
        )

        # Add tool usage instructions (critical for function calling)
        tool_instructions = """
<tool_usage>
## 工具使用规范

### 关键原则
**你必须通过 function calling 机制调用工具，而不是在文本中写工具调用代码。**
- 错误示例：在回复中写 `mcp_docgen__generate_document(format="pptx", ...)`
- 正确做法：使用 function calling 功能调用工具

### 调用流程
1. 简要说明你将要做什么
2. 调用相应的工具（系统自动处理）
3. 工具执行后，总结结果告知用户

### 常见工具
- `mcp_docgen__generate_document`: 一个工具覆盖四种格式 —— 用 `format` 参数
  在 docx / pptx / xlsx / pdf 之间选择。会自动规划结构、渲染、视觉复核，
  返回可下载的签名 URL。
- `web_search`: 搜索网络获取最新信息
- `execute_python`: 执行 Python 代码（数据分析、临时计算）

### 注意事项
- 文件生成完成后告知用户
- 不要在文本中输出 JSON 或函数调用语法
</tool_usage>"""

        return f"{base_prompt}\n\n{tool_instructions}"

    # Use the new Manus-style prompt as default (lazy initialization in _build_messages)
    # Set to None to trigger dynamic building with context
    DEFAULT_SYSTEM_PROMPT = None  # Will be built dynamically with build_default_system_prompt()

    # Context injection template
    CONTEXT_TEMPLATE = """## Relevant Context

The following information was retrieved from the knowledge base and may be helpful for answering the user's question:

{context}

---

Please use this context to inform your response when relevant. If the context doesn't contain the answer, you may rely on your general knowledge but should indicate this."""

    # Web search context template
    WEB_CONTEXT_TEMPLATE = """## Web Search Results

The following information was retrieved from the web and may provide up-to-date context:

{context}

---

Please use this web search context to inform your response when relevant."""

    def __init__(
        self,
        model_registry: ModelRegistry,
        kb_service: "KnowledgeClientLike | None" = None,
        tavily_api_key: str | None = None,
        session_manager: SessionManagerLike | None = None,
        context_config: ContextConfig | None = None,
        enable_rag_evaluation: bool = True,
        code_executor: CodeExecutorService | None = None,
        task_planner: TaskPlanner | None = None,
        tool_orchestrator: ToolOrchestrator | None = None,
        db: Any | None = None,  # DatabaseStorage for MemoryManager
        vlm_service: Any | None = None,  # DashScopeVLMService for image descriptions
        redis_client: Any | None = None,  # Redis client for caching
        memory_service: MemoryService | None = None,
        quality_guardrails: QualityGuardrails | None = None,
        tool_constraint_validator: ToolConstraintValidator | None = None,
        execution_gateway: Any | None = None,
        request_router: Any | None = None,
        kb_proxy: Any | None = None,
        # ADR-002: Tenant isolation services
        tenant_tool_policy: Any | None = None,
        tenant_mcp_config: Any | None = None,
        tool_audit: Any | None = None,
        # Bucket-B injection (Phase 4.2). Defaults are NoOp reference impls
        # from ai_gateway_core; the composition root in main.py passes in the
        # gateway's real recorder/storage concretes.
        usage_recorder: UsageRecorderLike = _DEFAULT_NOOP_USAGE_RECORDER,
        realtime_metrics: RealtimeMetricsLike = _DEFAULT_NOOP_REALTIME_METRICS,
        artifact_storage: ArtifactStorageLike = _DEFAULT_NOOP_ARTIFACT_STORAGE,
        file_storage: FileStorageLike = _DEFAULT_NOOP_FILE_STORAGE,
    ):
        self.model_registry = model_registry
        self.kb_service = kb_service or kb_proxy  # Use proxy when local KB unavailable
        # ADR-002: Tenant isolation
        self.tenant_tool_policy = tenant_tool_policy
        self.tenant_mcp_config = tenant_mcp_config
        self.tool_audit = tool_audit
        self.tavily_tool = TavilySearchTool(api_key=tavily_api_key)
        self.session_manager = session_manager
        self.context_manager = get_context_manager()
        self.context_config = context_config or ContextConfig()
        self.db = db  # Database storage for MemoryManager
        self.redis = redis_client
        self.memory_service = memory_service

        # Background task registry — keeps fire-and-forget tasks alive.
        # Python 3.11+ will GC tasks that have no strong reference, so any
        # task we launch via asyncio.create_task without awaiting MUST be
        # stored here. Tasks remove themselves via done_callback.
        self._background_tasks: set[asyncio.Task] = set()

        # Task planning and orchestration (Phase 2.4)
        # These are created on demand if not provided
        self._task_planner = task_planner
        self._tool_orchestrator = tool_orchestrator

        # Phase 3: RAG evaluation
        self.enable_rag_evaluation = enable_rag_evaluation
        self.rag_evaluator = get_rag_evaluator() if enable_rag_evaluation else None

        # Phase 4: Output guardrails
        self.output_guardrail = OutputGuardrail(
            max_length=10000,
            check_pii=True,
            check_hallucination=True,
        )

        # Code executor support
        self.code_executor = code_executor
        if self.code_executor:
            self._register_code_executor_tool()

        # Artifact storage (for persisting output files) — DI from composition root
        self.artifact_storage = artifact_storage
        # Bucket-B DI — recorders for per-request usage + realtime dashboards
        self.usage_recorder = usage_recorder
        self.realtime_metrics = realtime_metrics

        # File storage (for accessing user uploads from S3/OSS) — DI from composition root.
        # A NoOp default makes ``if self.file_storage:`` evaluate False (via __bool__),
        # preserving the legacy "not configured, fall back to local" semantics.
        self.file_storage = file_storage
        if self.file_storage:
            logger.info(
                f"[AssistantService] File storage initialized: backend={self.file_storage.config.backend.value}"
            )
        else:
            logger.info("[AssistantService] File storage not configured (using local fallback)")

        # KV-Cache optimization
        self.cache_optimizer = ContextCacheOptimizer(CacheConfig())

        # File processor for upload analysis
        # IMPORTANT: Use the same base path as FileStorageService to ensure path consistency
        storage_base_path = None
        if self.file_storage:
            storage_base_path = Path(self.file_storage.config.local_base_path)
            logger.info(
                f"[AssistantService] FileProcessor using storage_base_path: {storage_base_path}"
            )
        self.file_processor = create_file_processor(
            vlm_service=vlm_service,
            knowledge_service=kb_service,
            file_storage=self.file_storage,  # Pass file storage for remote access
            storage_base_path=storage_base_path,
            redis_client=redis_client,
        )

        # Per-session working memory with TTL auto-cleanup (1h expiry, max 5000 sessions)
        self._working_memories: TTLCache = TTLCache(maxsize=5000, ttl=3600)

        # Quality Guardrails (ensure content meets minimum quality standards)
        self.quality_guardrails = quality_guardrails or QualityGuardrails()
        self.tool_constraint_validator = tool_constraint_validator or ToolConstraintValidator()

        # Scenario Analyzer for intelligent scenario detection and analysis prompts
        # This enables "Manus-like" expert analysis capabilities
        self.scenario_analyzer = create_scenario_analyzer()

        # Built-in domain policy is disabled by default for generic assistant behavior.
        self.builtin_domain_policy_enabled = (
            os.getenv("ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED", "false").strip().lower() == "true"
        )
        self.domain_policy_resolver = (
            DomainPolicyResolver() if self.builtin_domain_policy_enabled else None
        )
        if self.builtin_domain_policy_enabled:
            logger.warning(
                "ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED=true: built-in domain policy is active."
            )

        # Assistant Gateway (policy routing + queue/approval/run lifecycle)
        # Keep this configurable to avoid hard-coded behavior.
        from .gateway import AssistantExecutionGateway, AssistantRequestRouter
        from .tool_invoker import create_tool_invoker

        gateway_enabled = True
        with contextlib.suppress(Exception):
            gateway_enabled = os.getenv("ASSISTANT_GATEWAY_ENABLED", "false").lower() == "true"

        self.request_router = request_router or AssistantRequestRouter()
        self.execution_gateway = execution_gateway or AssistantExecutionGateway(
            tool_invoker=create_tool_invoker(
                tenant_tool_policy=self.tenant_tool_policy,
                tenant_mcp_config=self.tenant_mcp_config,
                tool_audit=self.tool_audit,
            ),
            database=db,
            enabled=gateway_enabled,
        )

    def validate_generated_content(
        self,
        content: str,
        doc_type: DocumentType,
    ) -> ValidationResult:
        """
        Validate generated content against quality guardrails.

        Args:
            content: The generated content
            doc_type: Type of document

        Returns:
            ValidationResult with issues if any
        """
        return self.quality_guardrails.validate(content, doc_type)

    async def _resolve_domain_policy(
        self,
        user: UserContext,
        dataset_ids: list[str],
    ) -> tuple[ImamPolicy | None, list[dict[str, Any]]]:
        """Resolve domain policy based on dataset metadata."""
        if (
            not self.builtin_domain_policy_enabled
            or not self.domain_policy_resolver
            or not dataset_ids
            or not self.kb_service
        ):
            return None, []

        async def _load_dataset(ds_id: str) -> dict[str, Any] | None:
            try:
                return await self.kb_service.require_dataset_access(user, ds_id, required="viewer")
            except Exception as exc:
                logger.warning(f"Failed to load dataset {ds_id} for policy resolution: {exc}")
                return None

        results = await asyncio.gather(
            *[_load_dataset(ds_id) for ds_id in dataset_ids],
            return_exceptions=True,
        )
        datasets = [r for r in results if isinstance(r, dict)]
        policy = self.domain_policy_resolver.resolve(datasets)
        return policy, datasets

    async def _repair_with_policy(
        self,
        policy: ImamPolicy,
        user_message: str,
        context_text: str,
        answer: str,
        model_id: str,
        temperature: float,
        max_tokens: int | None,
        issues: list[str],
    ) -> str:
        """Attempt a single repair pass to satisfy policy constraints."""
        repair_instructions = policy.build_repair_instructions(issues)
        system_prompt = (
            "You are a compliance-focused editor. "
            "Revise the answer to meet the rules without adding external knowledge."
        )
        user_prompt = (
            f"Context:\n{context_text}\n\n"
            f"Question:\n{user_message}\n\n"
            f"Draft Answer:\n{answer}\n\n"
            f"Repair Instructions:\n{repair_instructions}\n"
        )
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        ]
        repaired, _ = await self.model_registry.chat(
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return repaired

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolCallValidation:
        """
        Validate a tool call against constraints.

        Args:
            tool_name: Name of the tool
            arguments: Tool arguments
            context: Current execution context

        Returns:
            ToolCallValidation with allowed status
        """
        return self.tool_constraint_validator.validate_tool_call(tool_name, arguments, context)

    async def _persist_artifacts(
        self,
        user: UserContext,
        session_id: str,
        output_files: list[dict[str, Any]],
        source: str = "code_execution",
    ) -> list[dict[str, Any]]:
        """
        Persist output files as artifacts and return updated file info with artifact IDs.

        Args:
            user: User context
            session_id: Session ID
            output_files: List of output files with filename, content_base64, mime_type, size_bytes
            source: Source of artifacts (code_execution, image_generation, etc.)

        Returns:
            Updated output_files list with artifact_id added to each file
        """
        from .artifacts import persist_output_files

        return await persist_output_files(
            artifact_storage=self.artifact_storage,
            user=user,
            session_id=session_id,
            output_files=output_files,
            source=source,
        )

    async def _ensure_session_exists(
        self,
        user: UserContext,
        session_id: str,
    ) -> None:
        """Ensure the assistant session exists before message persistence."""
        if not self.session_manager or not session_id:
            return

        existing = await self.session_manager.get(session_id)
        if existing:
            if existing.user_id != user.user_id or existing.tenant_id != user.tenant_id:
                raise PermissionDeniedError("Session does not belong to current user")
            if existing.service_id and existing.service_id != "__builtin_assistant__":
                raise PermissionDeniedError("Session is bound to a different service")
            return

        try:
            await self.session_manager.create(
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                service_id="__builtin_assistant__",
                session_id=session_id,
            )
        except Exception:
            # Handle concurrent creates for the same session_id.
            existing = await self.session_manager.get(session_id)
            if not existing:
                raise
            if existing.user_id != user.user_id or existing.tenant_id != user.tenant_id:
                raise PermissionDeniedError("Session does not belong to current user")
            if existing.service_id and existing.service_id != "__builtin_assistant__":
                raise PermissionDeniedError("Session is bound to a different service")

    async def chat_stream(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Stream a chat response with session persistence and context management.

        Args:
            user: User context for authentication/authorization
            session_id: Session ID for conversation tracking
            message: User's message
            config: Assistant configuration
            history: Previous conversation history (if None, loaded from session)
            persist_messages: Whether to persist messages to database

        Yields:
            AssistantStreamEvent objects with different event types:
            - context_retrieved: KB search results (if RAG enabled)
            - text_delta: Incremental text content
            - tool_call: Tool invocation (future)
            - tool_result: Tool response (future)
            - usage: Token usage statistics
            - done: Stream completion

        Context Management:
            - Applies sliding window (last 30 messages)
            - Token-aware truncation based on model context window
            - Preserves at least 6 recent messages
        """
        start_time = time.time()

        # ========== LATENCY DEBUG: Track timing for each step ==========
        logger.info(f"[LATENCY] chat_stream started at {start_time}")

        await self._ensure_session_exists(user=user, session_id=session_id)

        domain_policy, _ = await self._resolve_domain_policy(user, config.kb_dataset_ids)

        # IMPORTANT: Keep streaming for AgentLoop mode.
        # Previously, any domain policy would force buffered `chat()` path, causing
        # no real-time text deltas (TTFT ~= total duration).
        # For agent_loop, inject domain rules into system prompt and continue streaming.
        if domain_policy and config.use_agent_loop:
            domain_rules = domain_policy.scenario_rules()
            if domain_rules:
                existing_prompt = (config.system_prompt or "").strip()
                config.system_prompt = (
                    f"{existing_prompt}\n\n{domain_rules}" if existing_prompt else domain_rules
                )
            logger.info(
                "[DOMAIN POLICY] Applied domain rules in streaming mode (agent_loop), "
                "keeping SSE incremental delivery."
            )

        if domain_policy and not config.use_agent_loop:
            if persist_messages and self.session_manager:
                try:
                    await self.session_manager.add_message(
                        session_id=session_id,
                        role="user",
                        content=message,
                        metadata={"timestamp": datetime.utcnow().isoformat()},
                    )
                except Exception as exc:
                    logger.warning(f"Failed to persist user message (policy branch): {exc}")

            # For strict-domain assistants, use buffered generation to enforce policies.
            result = await self.chat(
                user=user,
                session_id=session_id,
                message=message,
                config=config,
                history=history,
                persist_messages=False,
            )
            if persist_messages and self.session_manager:
                try:
                    await self.session_manager.add_message(
                        session_id=session_id,
                        role="assistant",
                        content=result.get("content", ""),
                        metadata={
                            "timestamp": datetime.utcnow().isoformat(),
                            "model_id": config.model_id,
                            "contexts": result.get("contexts"),
                        },
                    )
                except Exception as exc:
                    logger.warning(f"Failed to persist assistant message (policy branch): {exc}")

            for ctx in result.get("contexts", []):
                yield AssistantStreamEvent(
                    event_type="context_retrieved",
                    data=ctx,
                )
            yield AssistantStreamEvent(
                event_type=StreamEventType.TEXT_DELTA.value,
                data=result.get("content", ""),
            )
            yield AssistantStreamEvent(
                event_type="usage",
                data=result.get("usage") or {"input_tokens": 0, "output_tokens": 0},
            )
            yield AssistantStreamEvent(
                event_type="done",
                data={
                    "session_id": session_id,
                    "duration_ms": result.get("duration_ms", 0),
                    "total_length": len(result.get("content", "")),
                },
            )
            return

        # ========== P2.2: Auto error recovery on user correction ==========
        if self._detect_user_correction(message):
            correction_ctx = (
                "The user has corrected your previous response. "
                "Acknowledge the correction briefly, re-execute any necessary tool calls "
                "with corrected parameters, and provide an updated answer. "
                "Do NOT just apologize — actually fix the issue."
            )
            existing = (config.system_prompt or "").strip()
            config.system_prompt = f"{existing}\n\n{correction_ctx}" if existing else correction_ctx

        # ========== Agent Loop Mode (Experimental) ==========
        # If use_agent_loop is enabled, delegate to the unified 8-step AgentLoop
        if config.use_agent_loop:
            async for event in self._execute_agent_loop(
                user=user,
                session_id=session_id,
                message=message,
                config=config,
                history=history,
            ):
                yield event
            return

        # ========== ReAct Phase 1: ANALYZING ==========
        # Detect task type for appropriate ReAct phase handling
        is_document_task = self._is_document_generation_task(message)

        # Emit initial status - analyzing
        yield AssistantStreamEvent(
            event_type=StreamEventType.STATUS,
            data={
                "phase": ReActPhase.ANALYZING.value,
                "message": "分析任务需求...",
                "is_document_task": is_document_task,
            },
        )

        # Enhanced scenario detection using ScenarioAnalyzer
        # This enables intelligent KB retrieval and expert-level analysis
        scenario_detection: ScenarioDetectionResult | None = None
        try:
            scenario_detection = self.scenario_analyzer.detect_scenario_fast(message)
            logger.info(
                f"[SCENARIO] Detected: primary={scenario_detection.primary_scenario.value}, "
                f"urgency={scenario_detection.urgency.value}, "
                f"confidence={scenario_detection.confidence:.2f}, "
                f"suggested_queries={scenario_detection.suggested_kb_queries[:2]}"
            )
        except Exception as e:
            logger.warning(f"Scenario detection failed: {e}")
            scenario_detection = None

        # Step 0: Load history from session if not provided (with timeout for TTFT optimization)
        step_start = time.time()
        if history is None and self.session_manager:
            try:
                # Add 500ms timeout to prevent slow DB queries from blocking TTFT
                session = await asyncio.wait_for(
                    self.session_manager.get(session_id),
                    timeout=0.5,  # 500ms timeout
                )
                if session and session.history:
                    history = _session_history_to_messages(session.history)
                else:
                    history = []
            except asyncio.TimeoutError:
                logger.warning("[LATENCY] Session history load timed out (>500ms), skipping")
                history = []
            except Exception as e:
                logger.warning(f"Failed to load session history: {e}")
                history = []
        else:
            history = history or []
        logger.info(f"[LATENCY] Step 0 (history load): {(time.time() - step_start) * 1000:.1f}ms")

        # Step 0.5: Apply context management (sliding window + token truncation)
        step_start = time.time()
        model_info = self.model_registry.get_model(config.model_id)
        model_context_window = model_info.context_window if model_info else 128000
        logger.info(
            f"[MODEL INFO] model_id={config.model_id}, "
            f"found={model_info is not None}, "
            f"supports_vision={model_info.supports_vision if model_info else 'N/A'}"
        )

        context_result = self.context_manager.process_history(
            history=history,
            model_context_window=model_context_window,
            config=self.context_config,
        )
        processed_history = context_result.messages

        if context_result.truncated_count > 0:
            logger.info(
                f"Session {session_id}: Context truncated {context_result.original_count} -> "
                f"{len(processed_history)} messages (tokens: {context_result.total_tokens})"
            )
        logger.info(f"[LATENCY] Step 0.5 (context mgmt): {(time.time() - step_start) * 1000:.1f}ms")

        # Step 0.6: Persist user message to session (fire-and-forget for lower latency)
        if persist_messages and self.session_manager:
            try:
                # Build metadata with file attachments if present
                user_msg_metadata: dict[str, Any] = {
                    "timestamp": datetime.utcnow().isoformat(),
                }
                # Save file paths as attachments for UI restoration
                if config.file_paths:
                    user_msg_metadata["attachments"] = [
                        {
                            "type": "image"
                            if any(
                                fp.lower().endswith(ext)
                                for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]
                            )
                            else "file",
                            "url": fp,
                            "filename": fp.split("/")[-1] if "/" in fp else fp,
                        }
                        for fp in config.file_paths
                    ]

                # Fire-and-forget with error logging: don't await, let it run in background
                # This saves 0.5-1s of latency by not blocking on DB write
                async def _persist_user_message():
                    try:
                        await self.session_manager.add_message(
                            session_id=session_id,
                            role="user",
                            content=message,
                            metadata=user_msg_metadata,
                        )
                    except Exception as persist_err:
                        logger.error(
                            f"[CRITICAL] User message persistence failed for session {session_id}: {persist_err}"
                        )

                _task = asyncio.create_task(_persist_user_message())
                # Keep a strong ref so Python 3.11+ doesn't GC it mid-flight
                self._background_tasks.add(_task)
                def _done(t: asyncio.Task) -> None:
                    self._background_tasks.discard(t)
                    if not t.cancelled() and t.exception() is not None:
                        logger.error(f"User message persist failed: {t.exception()}")
                _task.add_done_callback(_done)
            except Exception as e:
                logger.warning(f"Failed to persist user message: {e}")

        # ==========================================================================
        # PARALLEL EXECUTION: Memory Loading + KB Retrieval (latency optimization)
        # This reduces first-token latency by running these operations concurrently
        # ==========================================================================
        step_start = time.time()

        user_preferences: str | None = None
        retrieved_contexts: list[RetrievedContext] = []

        # Prepare coroutines for parallel execution
        memory_task = self._load_user_memory(user=user, session_id=session_id)

        kb_task = None
        kb_enabled = config.kb_mode == RAGMode.AUTO and config.kb_dataset_ids and self.kb_service
        logger.info(
            f"KB retrieval check - mode: {config.kb_mode}, "
            f"datasets: {config.kb_dataset_ids}, "
            f"kb_service: {self.kb_service is not None}"
        )
        if kb_enabled:
            # Emit status event before starting parallel tasks
            yield AssistantStreamEvent(
                event_type="status",
                data={"status": "searching_kb", "message": "Searching knowledge base..."},
            )
            logger.info(f"Starting KB retrieval for {len(config.kb_dataset_ids)} datasets")
            kb_task = self._retrieve_context(
                user=user,
                query=message,
                dataset_ids=config.kb_dataset_ids,
                top_k=config.kb_top_k,
                score_threshold=config.kb_score_threshold,
                include_images=config.kb_include_images,
            )

        # Run memory loading and KB retrieval in PARALLEL
        if kb_task:
            memory_result, kb_result = await asyncio.gather(
                memory_task, kb_task, return_exceptions=True
            )
        else:
            memory_result = await memory_task
            kb_result = None

        # Process memory result
        if not isinstance(memory_result, Exception):
            user_preferences, memory_data = memory_result
            if memory_data:
                yield AssistantStreamEvent(
                    event_type=StreamEventType.MEMORY_LOADED.value,
                    data={"preferences_loaded": True, "preferences": memory_data},
                )
        else:
            logger.warning(f"Memory loading failed: {memory_result}")

        # Process KB result
        if kb_result is not None:
            if not isinstance(kb_result, Exception):
                retrieved_contexts = kb_result
                for ctx in retrieved_contexts:
                    yield AssistantStreamEvent(
                        event_type="context_retrieved",
                        data={
                            "dataset_id": ctx.dataset_id,
                            "dataset_name": ctx.dataset_name,
                            "chunks": ctx.chunks,
                            "query": ctx.query,
                            "took_ms": ctx.took_ms,
                        },
                    )
            else:
                logger.warning(f"KB retrieval failed: {kb_result}")
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"KB retrieval failed: {str(kb_result)}", "recoverable": True},
                )
        logger.info(
            f"[LATENCY] Step 1 (memory + KB parallel): {(time.time() - step_start) * 1000:.1f}ms"
        )

        # Step 2: Web search if enabled
        step_start = time.time()
        web_search_context: str | None = None
        web_search_results_data: dict | None = None  # Store for persistence
        if config.web_search_enabled and self.tavily_tool.is_configured:
            yield AssistantStreamEvent(
                event_type="status",
                data={"status": "searching_web", "message": "Searching the web..."},
            )
            try:
                search_response = await self.tavily_tool.search(
                    query=message,
                    max_results=config.web_search_max_results,
                )
                web_search_context = self.tavily_tool.format_for_context(search_response)
                web_search_results_data = self.tavily_tool.format_for_display(search_response)
                yield AssistantStreamEvent(
                    event_type="web_search_results", data=web_search_results_data
                )
            except Exception as e:
                logger.warning(f"Web search failed: {e}")
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"Web search failed: {str(e)}", "recoverable": True},
                )
        logger.info(f"[LATENCY] Step 2 (web search): {(time.time() - step_start) * 1000:.1f}ms")

        # Step 2.5: Process uploaded files if any
        step_start = time.time()
        processed_files: ProcessedFiles | None = None
        model_supports_vision = model_info.supports_vision if model_info else False

        if config.file_paths:
            yield AssistantStreamEvent(
                event_type="status",
                data={"status": "processing_files", "message": "Analyzing uploaded files..."},
            )
            try:
                processed_files = await self.file_processor.process_files(
                    file_paths=config.file_paths,
                    session_id=session_id,
                    user=user,
                    model_supports_vision=model_supports_vision,
                )

                # Emit file processing event
                yield AssistantStreamEvent(
                    event_type=StreamEventType.FILE_PROCESSED.value,
                    data={
                        "image_count": len(processed_files.images),
                        "text_length": len(processed_files.text_content),
                        "description_count": len(processed_files.image_descriptions),
                        "requires_rag": processed_files.requires_rag,
                        "file_metadata": processed_files.file_metadata,
                    },
                )
                logger.info(
                    f"[FILE PROCESS] Processed {len(config.file_paths)} files: "
                    f"images={len(processed_files.images)}, "
                    f"text_chars={len(processed_files.text_content)}, "
                    f"descriptions={len(processed_files.image_descriptions)}, "
                    f"requires_rag={processed_files.requires_rag}"
                )

                # Handle case where document is too large for inline processing
                # This happens when text exceeds max_text_chars (32000)
                if processed_files.requires_rag and not processed_files.text_content:
                    logger.warning(
                        "[FILE PROCESS] Document too large for inline processing. "
                        "Uploaded file RAG not implemented - model won't see document content."
                    )
                    yield AssistantStreamEvent(
                        event_type="status",
                        data={
                            "status": "file_too_large",
                            "message": "文档内容较大，正在尝试处理核心部分...",
                        },
                    )
                    # For now, extract a truncated preview from metadata
                    for metadata in processed_files.file_metadata:
                        if metadata.get("truncated_preview"):
                            truncated = metadata["truncated_preview"]
                            processed_files.text_content = (
                                f"[文档内容过长，以下为前1000字符预览]\n{truncated}"
                            )
                            logger.info(
                                f"[FILE PROCESS] Using truncated preview: {len(truncated)} chars"
                            )
                            break

                # Handle case where document parsing failed
                # This happens when unstructured library is not installed or file format is unsupported
                if not processed_files.text_content and not processed_files.has_images:
                    parse_errors = []
                    for metadata in processed_files.file_metadata:
                        # Check both 'parse_error' and 'error' keys
                        error_msg = metadata.get("parse_error") or metadata.get("error")
                        if error_msg:
                            parse_errors.append(
                                f"- {metadata.get('file_name', 'unknown')}: {error_msg}"
                            )

                    if parse_errors:
                        error_msg = "文档解析失败:\n" + "\n".join(parse_errors)
                        logger.warning(f"[FILE PROCESS] Document parse errors: {parse_errors}")
                        yield AssistantStreamEvent(
                            event_type="status",
                            data={
                                "status": "file_parse_error",
                                "message": "文档解析遇到问题，请尝试其他格式",
                            },
                        )
                        # Add error message as text content so model can explain to user
                        processed_files.text_content = f"[文件处理错误]\n{error_msg}\n\n请告知用户文件解析失败，建议尝试其他格式（如PDF、TXT、DOCX）或确保文件内容完整。"
                    else:
                        # Files processed but no content extracted (shouldn't happen normally)
                        logger.warning(
                            f"[FILE PROCESS] Files processed but no content extracted. "
                            f"metadata={processed_files.file_metadata}"
                        )
            except Exception as e:
                logger.error(f"File processing failed: {e}", exc_info=True)
                yield AssistantStreamEvent(
                    event_type="error",
                    data={"message": f"File processing failed: {str(e)}", "recoverable": True},
                )
                # Create a ProcessedFiles with error message so the model can explain the issue
                processed_files = ProcessedFiles()
                error_details = str(e)
                if (
                    "remote storage not available" in error_details.lower()
                    or "file_storage is none" in error_details.lower()
                ):
                    processed_files.text_content = (
                        f"[文件处理错误]\n"
                        f"无法读取上传的文件。错误信息: {error_details}\n\n"
                        f"可能的原因:\n"
                        f"- 存储服务未正确配置 (请检查 FILE_STORAGE_BACKEND 环境变量)\n"
                        f"- 文件可能存储在远程存储(S3/OSS)但本地无法访问\n\n"
                        f"请告知用户文件读取失败，建议联系管理员检查存储配置。"
                    )
                else:
                    processed_files.text_content = (
                        f"[文件处理错误]\n"
                        f"处理上传文件时发生错误: {error_details}\n\n"
                        f"请告知用户文件处理失败，建议尝试:\n"
                        f"1. 使用其他格式的文件（如PDF、TXT、DOCX）\n"
                        f"2. 确保文件内容完整且未损坏\n"
                        f"3. 尝试较小的文件"
                    )
        logger.info(
            f"[LATENCY] Step 2.5 (file processing): {(time.time() - step_start) * 1000:.1f}ms"
        )

        # Step 2.6: Task Planning Mode (Phase 2.4)
        # If task planning is enabled, use the planner and orchestrator
        # for complex multi-step request execution
        planning_deferred = False
        if config.enable_task_planning:
            logger.info(f"[TASK PLANNING] Task planning enabled for session {session_id}")
            async for event in self._execute_with_planning(
                user=user,
                session_id=session_id,
                message=message,
                config=config,
                history=processed_history,
                retrieved_contexts=retrieved_contexts,
                web_search_context=web_search_context,
            ):
                if (
                    event.event_type == StreamEventType.STATUS.value
                    and isinstance(event.data, dict)
                    and event.data.get("status") == "plan_ready"
                ):
                    planning_deferred = True
                yield event

            if planning_deferred:
                return

            # After planning execution, we still need to generate the final response
            # using the collected results. The working memory contains all results.
            # Continue to normal model streaming with enhanced context from working memory
            working_memory = self.get_working_memory(session_id)
            if working_memory.collected_info:
                # Inject execution results into web search context for model
                results_summary = working_memory.to_markdown()
                if web_search_context:
                    web_search_context = web_search_context + "\n\n" + results_summary
                else:
                    web_search_context = results_summary
                logger.info("[TASK PLANNING] Injected execution results into context")

        # Step 3: Build messages (use processed_history with context management applied)
        messages = self._build_messages(
            message=message,
            history=processed_history,
            config=config,
            retrieved_contexts=retrieved_contexts,
            web_search_context=web_search_context,
            processed_files=processed_files,
            model_supports_vision=model_supports_vision,
            session_id=session_id,
            user_preferences=user_preferences,
            scenario_detection=scenario_detection,
        )

        # Step 4: Stream from model
        total_content = ""
        # Turn-level accumulators for activity-drawer persistence (legacy path).
        # These survive across iterations of the agentic tool loop below and
        # get serialized onto the final assistant message's metadata so the
        # frontend can rebuild the timeline on session reload. Without these,
        # the Activity drawer shows "No activity recorded · 0 steps" even
        # though thinking_delta and tool_call events streamed during the turn.
        total_thinking_content: str = ""
        turn_tool_calls: list[dict[str, Any]] = []
        turn_tool_results: list[dict[str, Any]] = []
        usage: dict[str, int] = {}

        # Get tools from registry (always load tools, not just when code executor exists)
        from .tools import get_tool_registry

        registry = get_tool_registry()
        tools = registry.get_openai_schemas()

        # Filter out search_knowledge_base tool when no KB datasets are configured
        # This prevents the LLM from calling the KB search tool when it would result
        # in searching ALL datasets (which is very slow - can cause 80+ second delays)
        if not config.kb_dataset_ids:
            tools = [
                t for t in tools if t.get("function", {}).get("name") != "search_knowledge_base"
            ]
            logger.info("KB search tool disabled - no datasets configured")

        # Filter out code executor tool if not available
        if not self.code_executor:
            tools = [t for t in tools if t.get("function", {}).get("name") != "execute_code"]

        # Native web search: when the chosen model has a built-in search mode
        # (Qwen `enable_search`, Gemini `google_search`, Anthropic web_search),
        # drop Tavily-backed `search_web` from the schema and forward the
        # provider-specific config to chat_stream. Mirrors the AgentLoop
        # filter at agent_loop.py:1926 — the legacy (use_agent_loop=False)
        # path must behave the same, otherwise Qwen 3.6 Plus keeps calling
        # `search_web` here even though it has native grounding.
        _legacy_model_info = self.model_registry.get_model(config.model_id)
        _legacy_native_search_cfg: dict[str, Any] | None = None
        if _legacy_model_info and getattr(
            _legacy_model_info, "supports_native_search", False
        ):
            # Gemini's `googleSearch` grounding is mutually exclusive with
            # `functionDeclarations` — mixing them 400s. The assistant always
            # runs with function tools in scope, so suppress native-search
            # for Google provider unconditionally; keep Tavily `search_web`.
            _legacy_provider = getattr(_legacy_model_info, "provider", None)
            _legacy_is_google = (
                getattr(_legacy_provider, "value", _legacy_provider) == "google"
            )
            if _legacy_is_google:
                logger.info(
                    "[NATIVE-SEARCH] (legacy path) Skipping google_search for "
                    "%s — cannot combine with functionDeclarations. Keeping "
                    "Tavily search_web as fallback.",
                    config.model_id,
                )
            else:
                _legacy_native_search_cfg = getattr(
                    _legacy_model_info, "native_search_config", None
                )
                before_n = len(tools)
                tools = [
                    t for t in tools if t.get("function", {}).get("name") != "search_web"
                ]
                if len(tools) != before_n:
                    logger.info(
                        "[NATIVE-SEARCH] (legacy path) Using %s built-in search; "
                        "dropped search_web tool.",
                        config.model_id,
                    )

        logger.info(f"Tools enabled for chat: {[t['function']['name'] for t in tools]}")

        # Agentic loop: handle tool calls until model finishes
        max_tool_iterations = 5
        current_messages = messages.copy()
        iteration = 0

        total_prep_time = (time.time() - start_time) * 1000
        logger.info(
            f"[LATENCY] Total preprocessing time: {total_prep_time:.1f}ms, starting LLM stream now"
        )

        # Determine thinking level and task type
        thinking_level = None
        is_ppt_request = False

        last_user_msg = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        ).lower()
        ppt_keywords = ["ppt", "slide", "powerpoint", "演示文稿", "幻灯片"]
        if any(kw in last_user_msg for kw in ppt_keywords):
            is_ppt_request = True
            logger.info(f"[TASK_DETECT] PPT generation request detected. Model: {config.model_id}")

            # Only apply thinking level for Gemini 3 models
            if "gemini-3" in config.model_id:
                thinking_level = "high"
                logger.info("[THINKING] High thinking level enabled for Gemini 3 PPT task")

        next_iteration_tool_config = None  # Initialize variable for tool forcing
        current_thinking_level = thinking_level  # Track thinking level for current iteration

        # TTFT (Time To First Token) measurement
        first_token_time: float | None = None

        while iteration < max_tool_iterations:
            iteration += 1
            tool_calls_accumulated: dict[int, dict[str, Any]] = {}
            finish_reason = None
            thought_signature_accumulated = None  # Track standalone thought signature

            # Log iteration context
            logger.info(
                f"[ITERATION {iteration}] Starting chat stream. ToolConfig: {bool(next_iteration_tool_config)}, Thinking: {current_thinking_level}"
            )

            # Emit ReAct phase status based on task type
            if iteration == 1:
                # First iteration - show THINKING or WRITING based on task type
                phase = ReActPhase.WRITING if is_document_task else ReActPhase.THINKING
                phase_message = "撰写内容中..." if is_document_task else "思考中..."
            else:
                # Subsequent iterations after tool execution
                phase = ReActPhase.THINKING
                phase_message = "继续思考..."

            yield AssistantStreamEvent(
                event_type=StreamEventType.STATUS,
                data={
                    "phase": phase.value,
                    "message": phase_message,
                },
            )

            try:
                async for delta in self.model_registry.chat_stream(
                    model_id=config.model_id,
                    messages=current_messages,
                    temperature=config.temperature,
                    max_tokens=config.max_tokens,
                    tools=tools,
                    thinking_level=current_thinking_level,
                    tool_config=next_iteration_tool_config,  # Pass tool_config if set
                    native_search_config=_legacy_native_search_cfg,
                ):
                    if delta.content:
                        # TTFT measurement: log time to first token
                        if first_token_time is None:
                            first_token_time = time.time()
                            ttft_ms = (first_token_time - start_time) * 1000
                            logger.info(f"[TTFT] First token received after {ttft_ms:.0f}ms")
                        total_content += delta.content
                        yield AssistantStreamEvent(event_type="text_delta", data=delta.content)

                    # Accumulate reasoning content (Qwen reasoning_content /
                    # Gemini thought parts) for activity-drawer persistence.
                    # The legacy path did not previously surface thinking to
                    # clients here, but the aggregate still needs to reach
                    # the DB so the Activity drawer can rebuild the timeline
                    # on session reload.
                    if delta.thinking_content:
                        total_thinking_content += delta.thinking_content
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.THINKING_DELTA.value,
                            data=delta.thinking_content,
                        )

                    if delta.thought_signature:
                        thought_signature_accumulated = delta.thought_signature
                        logger.info(
                            f"[GEMINI3] Received standalone thought_signature of length {len(thought_signature_accumulated)}"
                        )

                    if delta.tool_calls:
                        # Accumulate tool call chunks
                        for tc in delta.tool_calls:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_accumulated:
                                tool_calls_accumulated[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.get("id"):
                                tool_calls_accumulated[idx]["id"] = tc["id"]
                            if tc.get("function", {}).get("name"):
                                tool_calls_accumulated[idx]["function"]["name"] = tc["function"][
                                    "name"
                                ]
                            if tc.get("function", {}).get("arguments"):
                                tool_calls_accumulated[idx]["function"]["arguments"] += tc[
                                    "function"
                                ]["arguments"]
                            # CRITICAL: Preserve thoughtSignature for Gemini 3
                            # Must be passed back in subsequent requests
                            if tc.get("thoughtSignature"):
                                tool_calls_accumulated[idx]["thoughtSignature"] = tc[
                                    "thoughtSignature"
                                ]

                        yield AssistantStreamEvent(event_type="tool_call", data=delta.tool_calls)

                    if delta.usage:
                        usage.update(delta.usage)

                    if delta.finish_reason:
                        finish_reason = delta.finish_reason

            except Exception as e:
                logger.error(f"Model streaming failed: {e}")
                yield AssistantStreamEvent(
                    event_type="error", data={"message": str(e), "recoverable": False}
                )
                return

            # Add assistant response to history
            assistant_msg = ChatMessage(
                role="assistant",
                content=total_content,
                thought_signature=thought_signature_accumulated,
            )
            if tool_calls_accumulated:
                assistant_msg.tool_calls = [
                    tool_calls_accumulated[idx] for idx in sorted(tool_calls_accumulated.keys())
                ]
            current_messages.append(assistant_msg)

            # Check if we should execute tools
            # IMPORTANT: Execute tools if we have ANY accumulated tool calls, regardless of finish_reason
            # Some providers (like Gemini) return finish_reason="stop" even when making tool calls
            logger.info(
                f"[DEBUG] Iteration {iteration}: finish_reason={finish_reason}, tool_calls_accumulated={list(tool_calls_accumulated.keys())}, is_ppt_request={is_ppt_request}"
            )

            # If we have tool calls, execute them regardless of finish_reason
            if not tool_calls_accumulated:
                # No tool calls - check if we need self-correction for PPT
                # Self-correction logic for PPT generation
                # Trigger if it's a PPT request, first iteration, and no tool calls were made
                # This works for ALL models (Gemini Flash, Pro, etc.)
                if is_ppt_request and iteration == 1:
                    logger.info(
                        f"[SELF-CORRECTION] PPT task finished without tool call (Model: {config.model_id}). Forcing tool call iteration."
                    )

                    # Append a system message to force tool execution
                    correction_msg = ChatMessage(
                        role="user",
                        content="规划已完成。请立即调用 `generate_pptx` 工具，将上述大纲转换为 JSON 参数生成文件。无需再解释。",
                    )
                    current_messages.append(correction_msg)

                    # Force tool execution using tool_config
                    tool_config = {
                        "functionCallingConfig": {
                            "mode": "ANY",
                            "allowedFunctionNames": ["generate_pptx"],
                        }
                    }
                    logger.info(
                        "[SELF-CORRECTION] Enabling forced tool execution for generate_pptx and disabling thinking"
                    )

                    # Emit a status event to let user know we are proceeding
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.STATUS,
                        data={
                            "phase": ReActPhase.EXECUTING.value,
                            "message": "大纲规划完成，正在生成文件...",
                        },
                    )

                    # Update variables for next iteration
                    next_iteration_tool_config = tool_config

                    # Optimization: Use lower thinking level for the mechanical tool call step
                    # This saves tokens and reduces latency since the plan is already done
                    current_thinking_level = "minimal" if "flash" in config.model_id else "low"

                    logger.info(
                        f"[SELF-CORRECTION] Setting thinking_level='{current_thinking_level}' for tool execution"
                    )

                    continue

                # No tool calls and not PPT self-correction, we're done
                yield AssistantStreamEvent(
                    event_type="finish", data={"reason": finish_reason or "stop"}
                )
                break

            # Execute tools - we have tool calls to process
            tool_results = []
            for idx in sorted(tool_calls_accumulated.keys()):
                tc = tool_calls_accumulated[idx]
                tool_name = tc["function"]["name"]
                tool_args_str = tc["function"]["arguments"]
                tool_id = tc["id"]

                # Emit ReAct EXECUTING phase status
                yield AssistantStreamEvent(
                    event_type=StreamEventType.STATUS,
                    data={
                        "phase": ReActPhase.EXECUTING.value,
                        "message": f"执行 {tool_name}...",
                        "task_id": tool_id,
                    },
                )

                try:
                    import json as json_module

                    tool_args = json_module.loads(tool_args_str) if tool_args_str else {}
                except json_module.JSONDecodeError:
                    tool_args = {}

                # Execute tool
                if tool_name == "execute_python_code" and self.code_executor:
                    code = tool_args.get("code", "")
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.CODE_EXECUTION_START,
                        data={"execution_id": tool_id, "language": "python", "code": code},
                    )

                    try:
                        # Prepare input files and KB documents for code execution
                        input_files, kb_documents = await self._prepare_code_execution_files(
                            file_paths=config.file_paths if hasattr(config, "file_paths") else None,
                            retrieved_contexts=retrieved_contexts
                            if "retrieved_contexts" in dir()
                            else None,
                        )

                        result = await self.code_executor.execute(
                            code=code,
                            input_files=input_files,
                            kb_documents=kb_documents,
                        )
                        success = result.is_success()
                        output = (
                            result.stdout
                            if success
                            else f"Error: {result.stderr or result.error_message}"
                        )

                        # Prepare output files
                        output_files = (
                            [
                                {
                                    "filename": f.filename,
                                    "content_base64": f.to_base64(),
                                    "mime_type": f.mime_type,
                                    "size_bytes": f.size_bytes,
                                }
                                for f in result.output_files
                            ]
                            if result.output_files
                            else []
                        )

                        # Persist artifacts to storage
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="code_execution",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.CODE_EXECUTION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": success,
                                "stdout": result.stdout,
                                "stderr": result.stderr,
                                "execution_time_ms": result.duration_ms,
                                "output_files": output_files,
                            },
                        )

                        # Send ARTIFACT_CREATED events for persisted artifacts
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": file_info.get(
                                            "type",
                                            "image"
                                            if file_info.get("mime_type", "").startswith("image/")
                                            else "file",
                                        ),
                                        "format": file_info.get(
                                            "format",
                                            file_info.get("mime_type", "").split("/")[-1]
                                            if file_info.get("mime_type")
                                            else "bin",
                                        ),
                                        "title": file_info.get("filename", "output"),
                                        "filename": file_info.get("filename"),
                                        "mime_type": file_info.get("mime_type"),
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "code_execution",
                                        "download_url": file_info.get("download_url"),
                                    },
                                )

                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": output,
                            }
                        )
                    except Exception as e:
                        logger.error(f"Code execution failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            },
                        )
                        # Add rich error context to tool results for model
                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            }
                        )

                elif tool_name == "generate_image":
                    # Image generation with streaming events
                    prompt = tool_args.get("prompt", "")
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.IMAGE_GENERATION_START,
                        data={"execution_id": tool_id, "prompt": prompt},
                    )

                    try:
                        from .tools import ToolCallRequest, get_tool_registry

                        registry = get_tool_registry()
                        tool_result = await registry.execute(
                            ToolCallRequest(
                                call_id=tool_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                                user=user,
                            )
                        )

                        # Persist generated images as artifacts
                        output_files = tool_result.output_files or []
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="image_generation",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.IMAGE_GENERATION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": tool_result.success,
                                "result": tool_result.result,
                                "error": tool_result.error,
                                "output_files": output_files,
                                "duration_ms": tool_result.duration_ms,
                            },
                        )

                        # Send ARTIFACT_CREATED events for persisted artifacts
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "image",
                                        "format": file_info.get(
                                            "format",
                                            file_info.get("mime_type", "").split("/")[-1]
                                            if file_info.get("mime_type")
                                            else "png",
                                        ),
                                        "title": file_info.get("filename", "generated_image"),
                                        "filename": file_info.get("filename"),
                                        "mime_type": file_info.get("mime_type"),
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "image_generation",
                                        "download_url": file_info.get("download_url"),
                                    },
                                )

                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": tool_result.result
                                if tool_result.success
                                else f"Error: {tool_result.error}",
                            }
                        )
                    except Exception as e:
                        logger.error(f"Image generation failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            },
                        )
                        # Add rich error context to tool results for model
                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            }
                        )

                elif tool_name == "generate_document":
                    # Document generation with streaming events
                    title = tool_args.get("title", "Document")
                    tool_args.get("content", "")
                    format_type = tool_args.get("format", "docx")

                    yield AssistantStreamEvent(
                        event_type=StreamEventType.DOCUMENT_GENERATION_START,
                        data={"execution_id": tool_id, "title": title, "format": format_type},
                    )

                    try:
                        from .tools import ToolCallRequest, get_tool_registry

                        registry = get_tool_registry()
                        tool_result = await registry.execute(
                            ToolCallRequest(
                                call_id=tool_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                                user=user,
                            )
                        )

                        # Persist generated documents as artifacts
                        output_files = tool_result.output_files or []
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="document_generation",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.DOCUMENT_GENERATION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": tool_result.success,
                                "result": tool_result.result,
                                "error": tool_result.error,
                                "output_files": output_files,
                                "duration_ms": tool_result.duration_ms,
                            },
                        )

                        # Send ARTIFACT_CREATED events for persisted documents
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                mime_type = file_info.get("mime_type", "")
                                doc_format = file_info.get("format", format_type)  # docx, pdf, md
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "document",
                                        "format": doc_format,
                                        "title": file_info.get("filename", title),
                                        "filename": file_info.get("filename"),
                                        "mime_type": mime_type,
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "document_generation",
                                        "download_url": file_info.get("download_url"),
                                    },
                                )

                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": tool_result.result
                                if tool_result.success
                                else f"Error: {tool_result.error}",
                            }
                        )
                    except Exception as e:
                        logger.error(f"Document generation failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            },
                        )
                        # Add rich error context to tool results for model
                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            }
                        )

                elif tool_name == "generate_pptx":
                    # PPTX generation with streaming events (Manus-style workflow)
                    pptx_start_time = time.time()
                    title = tool_args.get("title", "Presentation")
                    slides = tool_args.get("slides", [])
                    theme = tool_args.get("theme", "professional")
                    step_id = f"pptx-{tool_id}"

                    # Emit Manus-style STEP_STARTED for task panel visualization
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.STEP_STARTED,
                        data={
                            "step_id": step_id,
                            "title": f"生成PPT: {title}",
                            "description": f"创建 {len(slides)} 页演示文稿",
                            "icon": "ppt",
                            "timestamp": int(time.time() * 1000),
                        },
                    )

                    # Emit Manus-style OUTLINE_READY event before generation
                    # This enables the frontend to display a slide outline preview
                    outline_slides = []
                    for i, slide in enumerate(slides, start=1):
                        slide_type = slide.get("layout", "content")
                        # Map layout to type
                        type_map = {
                            "title_slide": "title",
                            "title": "title",
                            "content": "content",
                            "two_column": "two_column",
                            "section_header": "section",
                            "section": "section",
                            "blank": "blank",
                        }
                        outline_slides.append(
                            {
                                "number": i,
                                "title": slide.get("title", f"Slide {i}"),
                                "subtitle": slide.get("subtitle"),
                                "type": type_map.get(slide_type, "content"),
                                "bulletCount": len(slide.get("bullets", []))
                                if slide.get("bullets")
                                else 0,
                            }
                        )

                    yield AssistantStreamEvent(
                        event_type=StreamEventType.OUTLINE_READY,
                        data={
                            "outline": {
                                "title": title,
                                "slides": outline_slides,
                                "theme": theme,
                                "totalSlides": len(slides),
                            },
                            "format": "pptx",
                        },
                    )

                    yield AssistantStreamEvent(
                        event_type=StreamEventType.DOCUMENT_GENERATION_START,
                        data={"execution_id": tool_id, "title": title, "format": "pptx"},
                    )

                    try:
                        from .tools import ToolCallRequest, get_tool_registry

                        registry = get_tool_registry()
                        tool_result = await registry.execute(
                            ToolCallRequest(
                                call_id=tool_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                                user=user,
                            )
                        )

                        # Persist generated PPTX as artifacts
                        output_files = tool_result.output_files or []
                        if output_files:
                            output_files = await self._persist_artifacts(
                                user=user,
                                session_id=session_id,
                                output_files=output_files,
                                source="pptx_generation",
                            )

                        yield AssistantStreamEvent(
                            event_type=StreamEventType.DOCUMENT_GENERATION_RESULT,
                            data={
                                "execution_id": tool_id,
                                "success": tool_result.success,
                                "result": tool_result.result,
                                "error": tool_result.error,
                                "output_files": output_files,
                                "duration_ms": tool_result.duration_ms,
                            },
                        )

                        # Send ARTIFACT_CREATED events for persisted PPTX
                        for file_info in output_files:
                            if file_info.get("artifact_id"):
                                yield AssistantStreamEvent(
                                    event_type=StreamEventType.ARTIFACT_CREATED,
                                    data={
                                        "artifact_id": file_info["artifact_id"],
                                        "type": "document",
                                        "format": "pptx",
                                        "title": file_info.get("filename", title),
                                        "filename": file_info.get("filename"),
                                        "mime_type": file_info.get(
                                            "mime_type",
                                            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                                        ),
                                        "size_bytes": file_info.get("size_bytes"),
                                        "source": "pptx_generation",
                                        "download_url": file_info.get("download_url"),
                                    },
                                )

                        # Emit STEP_FINISHED for task panel visualization
                        pptx_duration_ms = int((time.time() - pptx_start_time) * 1000)
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.STEP_FINISHED,
                            data={
                                "step_id": step_id,
                                "status": "completed" if tool_result.success else "failed",
                                "result": f"PPT已生成: {title} ({len(slides)}页)",
                                "duration_ms": pptx_duration_ms,
                                "timestamp": int(time.time() * 1000),
                            },
                        )

                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": tool_result.result
                                if tool_result.success
                                else f"Error: {tool_result.error}",
                            }
                        )
                    except Exception as e:
                        logger.error(f"PPTX generation failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            },
                        )
                        # Emit STEP_FINISHED with failed status
                        pptx_duration_ms = int((time.time() - pptx_start_time) * 1000)
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.STEP_FINISHED,
                            data={
                                "step_id": step_id,
                                "status": "failed",
                                "error": str(e),
                                "duration_ms": pptx_duration_ms,
                                "timestamp": int(time.time() * 1000),
                            },
                        )
                        # Add rich error context to tool results for model
                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            }
                        )

                else:
                    # Execute other registered tools via registry
                    try:
                        from .tools import ToolCallRequest, get_tool_registry

                        registry = get_tool_registry()

                        if registry.get_tool(tool_name):
                            tool_result = await registry.execute(
                                ToolCallRequest(
                                    call_id=tool_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user=user,
                                )
                            )
                            tool_results.append(
                                {
                                    "tool_call_id": tool_id,
                                    "role": "tool",
                                    "content": tool_result.result
                                    if tool_result.success
                                    else f"Error: {tool_result.error}",
                                }
                            )
                        else:
                            # Unknown tool - create structured error
                            error_info = ToolErrorInfo(
                                tool_name=tool_name,
                                tool_call_id=tool_id,
                                error_type="UnknownToolError",
                                error_message=f"Tool '{tool_name}' is not registered",
                                arguments=tool_args,
                                suggestion="Check available tools. The tool may have been misspelled or is not available.",
                            )
                            yield AssistantStreamEvent(
                                event_type=StreamEventType.TOOL_ERROR,
                                data={
                                    "tool_name": error_info.tool_name,
                                    "tool_call_id": error_info.tool_call_id,
                                    "error_type": error_info.error_type,
                                    "error_message": error_info.error_message,
                                    "suggestion": error_info.suggestion,
                                },
                            )
                            tool_results.append(
                                {
                                    "tool_call_id": tool_id,
                                    "role": "tool",
                                    "content": error_info.to_rich_context(),
                                }
                            )
                    except Exception as e:
                        logger.error(f"Tool {tool_name} execution failed: {e}", exc_info=True)
                        # Create structured error for better agent recovery
                        error_info = self._create_tool_error(
                            tool_name=tool_name,
                            tool_call_id=tool_id,
                            error=e,
                            arguments=tool_args,
                        )
                        # Emit error event for frontend
                        yield AssistantStreamEvent(
                            event_type=StreamEventType.TOOL_ERROR,
                            data={
                                "tool_name": error_info.tool_name,
                                "tool_call_id": error_info.tool_call_id,
                                "error_type": error_info.error_type,
                                "error_message": error_info.error_message,
                                "suggestion": error_info.suggestion,
                            },
                        )
                        # Add rich error context to tool results for model
                        tool_results.append(
                            {
                                "tool_call_id": tool_id,
                                "role": "tool",
                                "content": error_info.to_rich_context(),
                            }
                        )

            # Accumulate tool calls + results at the turn level so the final
            # assistant message persists everything the Activity drawer needs.
            # (Done once per iteration, after tool execution settles.)
            for _idx in sorted(tool_calls_accumulated.keys()):
                _tc = tool_calls_accumulated[_idx]
                try:
                    _args = (
                        json.loads(_tc.get("function", {}).get("arguments") or "{}")
                        if _tc.get("function", {}).get("arguments")
                        else {}
                    )
                    if not isinstance(_args, dict):
                        _args = {}
                except (json.JSONDecodeError, ValueError, TypeError):
                    _args = {}
                turn_tool_calls.append(
                    {
                        "id": _tc.get("id", ""),
                        "name": _tc.get("function", {}).get("name", ""),
                        "arguments": _args,
                        "status": "completed",
                    }
                )
            for _tr in tool_results:
                _content = _tr.get("content") if isinstance(_tr, dict) else None
                # Cap both string and non-string payloads at ~4000 chars.
                # Non-string values (dicts/lists from some providers) were
                # previously stored verbatim and could balloon the session
                # JSONB.
                if isinstance(_content, str):
                    _stored = _content[:4000]
                elif _content is None:
                    _stored = None
                else:
                    try:
                        _stored = json.dumps(_content, ensure_ascii=False)[:4000]
                    except (TypeError, ValueError):
                        _stored = str(_content)[:4000]
                turn_tool_results.append(
                    {
                        "tool_call_id": _tr.get("tool_call_id") if isinstance(_tr, dict) else None,
                        "name": None,
                        "result": _stored,
                        "error": None,
                        "duration_ms": None,
                    }
                )

            # Emit ReAct OBSERVING phase status after tool execution
            yield AssistantStreamEvent(
                event_type=StreamEventType.STATUS,
                data={
                    "phase": ReActPhase.OBSERVING.value,
                    "message": "分析工具执行结果...",
                },
            )

            # Add assistant message with tool calls and tool results
            current_messages.append(
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        tool_calls_accumulated[idx] for idx in sorted(tool_calls_accumulated.keys())
                    ],
                )
            )
            for tr in tool_results:
                current_messages.append(
                    ChatMessage(
                        role="tool",
                        content=tr["content"],
                        tool_call_id=tr["tool_call_id"],
                    )
                )

            logger.info(
                f"Tool iteration {iteration}: executed {len(tool_results)} tools, continuing..."
            )

        # Safeguard: Handle empty content after agentic loop
        # If model generated output tokens but no text content, it likely only produced
        # tool calls or thinking tokens. Provide a helpful fallback message.
        if not total_content and usage.get("output_tokens", 0) > 0:
            logger.warning(
                f"[EMPTY CONTENT] Model generated {usage.get('output_tokens', 0)} output tokens "
                f"but no text content. Iterations: {iteration}, max: {max_tool_iterations}"
            )
            # Check if there were file attachments - might need to explain what happened
            if config.file_paths:
                fallback_message = (
                    "抱歉，我无法正确解析您上传的文件内容。"
                    "这可能是因为文件格式不受支持或文件内容无法提取。"
                    "请尝试上传其他格式的文件（如 PDF、TXT、DOCX）或确保文件不是空的。"
                )
            else:
                fallback_message = (
                    "抱歉，我无法生成有效的回复。请尝试重新提问或换一种方式描述您的需求。"
                )
            total_content = fallback_message
            # TTFT measurement for fallback (first_token_time would be None)
            if first_token_time is None:
                first_token_time = time.time()
                ttft_ms = (first_token_time - start_time) * 1000
                logger.info(
                    f"[TTFT] Fallback message after {ttft_ms:.0f}ms (no content from model)"
                )
            yield AssistantStreamEvent(event_type="text_delta", data=fallback_message)

        # Step 5: Persist assistant response to session
        if persist_messages and self.session_manager and total_content:
            try:
                # Serialize contexts for persistence
                contexts_data = []
                for ctx in retrieved_contexts:
                    contexts_data.append(
                        {
                            "dataset_id": ctx.dataset_id,
                            "dataset_name": ctx.dataset_name,
                            "chunks": ctx.chunks,
                            "query": ctx.query,
                            "took_ms": ctx.took_ms,
                            "avg_score": ctx.avg_score,
                            "top_score": ctx.top_score,
                        }
                    )

                # Cap thinking payload to avoid JSONB bloat (session metadata
                # hard cap is 1MB). Reasoning models can emit 10k+ chars; keep
                # head + tail so reload still shows context.
                _persisted_thinking: str | None = None
                if total_thinking_content:
                    _stripped = total_thinking_content.strip()
                    if _stripped:
                        if len(_stripped) > 16000:
                            _persisted_thinking = (
                                _stripped[:8000]
                                + "\n\n…[truncated]…\n\n"
                                + _stripped[-8000:]
                            )
                        else:
                            _persisted_thinking = _stripped

                _metadata = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "model_id": config.model_id,
                    "usage": usage,
                    "contexts": contexts_data if contexts_data else None,
                    "web_search_results": web_search_results_data,
                    # Activity-drawer restoration fields (None when absent
                    # so no DB/UX overhead for turns without reasoning/tools).
                    "thinking_content": _persisted_thinking,
                    "tool_calls": turn_tool_calls or None,
                    "tool_results": turn_tool_results or None,
                }

                # Final safety net mirroring agent_loop.py: shed Activity
                # fields in priority order if metadata would push the row
                # past the 1MB JSONB ceiling. Losing a drawer view beats
                # losing the entire assistant message.
                _size_ceiling = 800_000
                for _shed in ("tool_results", "tool_calls", "thinking_content"):
                    try:
                        _size = len(json.dumps(_metadata, default=str))
                    except (TypeError, ValueError):
                        break
                    if _size <= _size_ceiling:
                        break
                    if _metadata.get(_shed) is not None:
                        logger.warning(
                            "[persist] metadata %d bytes over ceiling; "
                            "shedding %s",
                            _size,
                            _shed,
                        )
                        _metadata[_shed] = None

                await self.session_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=total_content,
                    metadata=_metadata,
                )
            except Exception as e:
                logger.warning(f"Failed to persist assistant message: {e}")

        # Step 6: RAG Evaluation (Phase 3)
        rag_evaluation: RAGEvaluation | None = None
        citations: list[Citation] = []

        if self.enable_rag_evaluation and retrieved_contexts and total_content:
            try:
                # Flatten all chunks for evaluation
                all_chunks = []
                dataset_names = {}
                total_retrieval_time = 0.0

                for ctx in retrieved_contexts:
                    dataset_names[ctx.dataset_id] = ctx.dataset_name
                    total_retrieval_time += ctx.took_ms
                    for chunk in ctx.chunks:
                        all_chunks.append(
                            {
                                **chunk,
                                "dataset_id": ctx.dataset_id,
                            }
                        )

                # Evaluate RAG quality
                rag_metrics = self.rag_evaluator.evaluate(
                    query=message,
                    response=total_content,
                    retrieved_chunks=all_chunks,
                    retrieval_time_ms=total_retrieval_time,
                )

                # Extract citations
                citations = self.rag_evaluator.extract_citations(
                    response=total_content,
                    retrieved_chunks=all_chunks,
                    dataset_names=dataset_names,
                )

                rag_evaluation = RAGEvaluation(
                    metrics=rag_metrics,
                    citations=citations,
                    quality_score=rag_metrics.quality_score,
                    grounding_ratio=rag_metrics.response_grounding,
                )

                # Emit RAG evaluation event
                yield AssistantStreamEvent(
                    event_type="rag_evaluation",
                    data={
                        "quality_score": rag_metrics.quality_score,
                        "quality_breakdown": rag_metrics.quality_breakdown,
                        "chunks_retrieved": rag_metrics.total_chunks_retrieved,
                        "chunks_used": rag_metrics.chunks_used,
                        "response_grounding": rag_metrics.response_grounding,
                        "citations": [c.to_dict() for c in citations],
                        "evaluation_time_ms": rag_metrics.evaluation_time_ms,
                    },
                )

                logger.info(
                    f"RAG evaluation: quality={rag_metrics.quality_score:.1f}, "
                    f"grounding={rag_metrics.response_grounding:.2f}, "
                    f"citations={len(citations)}"
                )

            except Exception as e:
                logger.warning(f"RAG evaluation failed: {e}")

        # Step 6.5: Output validation (Phase 4)
        output_warnings: list[str] = []
        if total_content:
            # Build context for hallucination check
            context_text = ""
            for ctx in retrieved_contexts:
                for chunk in ctx.chunks:
                    context_text += chunk.get("content", "") + "\n"

            output_warnings = self.output_guardrail.validate(
                output=total_content,
                context=context_text if context_text else None,
            )

            if retrieved_contexts:
                output_warnings.extend(self._validate_citations(total_content, citations))

            if output_warnings:
                logger.warning(f"Output warnings: {output_warnings}")
                yield AssistantStreamEvent(
                    event_type="output_warnings", data={"warnings": output_warnings}
                )

        # Step 6.6: Extract user preferences for memory
        if self.memory_service and message:
            try:
                from .memory.preference_extractor import (
                    extract_preferences,
                    merge_preferences,
                    split_memory_updates,
                )

                extracted = extract_preferences(message)
                preference_updates, fact_updates = split_memory_updates(extracted)

                if preference_updates:
                    existing_preferences = await self.memory_service.get_user_memory(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        key="preferences",
                    )
                    await self.memory_service.set_user_memory(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        key="preferences",
                        value=merge_preferences(existing_preferences, preference_updates),
                        metadata={"source": "auto_extract", "namespace": "preferences"},
                    )

                for key, value in fact_updates.items():
                    await self.memory_service.set_user_memory(
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        key=key,
                        value=value,
                        metadata={"source": "auto_extract", "namespace": "profile"},
                    )
            except Exception as e:
                logger.debug(f"Preference extraction failed: {e}")

        # Prepare legacy memory manager fallback (when MemoryService not configured)
        memory_manager = None
        if not self.memory_service and self.db:
            memory_manager = MemoryManager(
                db=self.db,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                session_id=session_id,
            )

        # Step 6.7: Store session memory for future context
        if memory_manager and total_content:
            try:
                # Store last query topic for context continuity
                await memory_manager.remember(
                    key="last_query_topic",
                    value={"query": message[:100], "timestamp": datetime.utcnow().isoformat()},
                    layer="session",
                )
                logger.debug(f"Stored session memory for query: {message[:50]}...")
            except Exception as e:
                logger.debug(f"Failed to store session memory: {e}")

        # Step 7: Emit final events
        elapsed_ms = (time.time() - start_time) * 1000

        if usage:
            yield AssistantStreamEvent(event_type="usage", data=usage)

            # Emit cache metrics event
            try:
                provider = self._get_provider_from_model(config.model_id)
                cache_metrics = self.cache_optimizer.parse_cache_metrics(usage, provider)
                if cache_metrics.cached_tokens > 0:
                    yield AssistantStreamEvent(
                        event_type=StreamEventType.CACHE_METRICS.value,
                        data={
                            "layer1_hit": cache_metrics.layer1_hit,
                            "layer2_hit": cache_metrics.layer2_hit,
                            "total_input_tokens": cache_metrics.total_input_tokens,
                            "cached_tokens": cache_metrics.cached_tokens,
                            "cache_hit_rate": cache_metrics.cache_hit_rate,
                            "estimated_savings_usd": cache_metrics.estimated_savings_usd,
                            "system_prefix_hash": cache_metrics.system_prefix_hash,
                        },
                    )
                    logger.info(
                        f"Cache metrics: {cache_metrics.cached_tokens}/{cache_metrics.total_input_tokens} tokens cached "
                        f"({cache_metrics.cache_hit_rate:.1%}), savings: ${cache_metrics.estimated_savings_usd:.4f}"
                    )
            except Exception as e:
                logger.warning(f"Failed to parse cache metrics: {e}")

            # Record usage to database for billing/analytics
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            try:
                usage_recorder = self.usage_recorder
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    service_id="__builtin_assistant__",
                    provider=config.model_provider.value,
                    latency_ms=int(elapsed_ms),
                    request_type="chat",
                    metadata={
                        "session_id": session_id,
                        "kb_datasets": config.kb_dataset_ids if retrieved_contexts else [],
                        "web_search": config.web_search_enabled,
                    },
                )
                logger.debug(f"Recorded usage: {usage} for user {user.user_id}")
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")

            # Update real-time metrics in Redis for dashboard
            try:
                realtime_metrics = self.realtime_metrics
                if realtime_metrics and (input_tokens > 0 or output_tokens > 0):
                    await realtime_metrics.record_token_usage(input_tokens, output_tokens)
                    logger.debug(
                        f"Updated realtime token metrics: input={input_tokens}, output={output_tokens}"
                    )
            except Exception as e:
                logger.warning(f"Failed to update realtime metrics: {e}")

        # Emit ReAct COMPLETING phase status before done
        yield AssistantStreamEvent(
            event_type=StreamEventType.STATUS,
            data={
                "phase": ReActPhase.COMPLETING.value,
                "message": "完成",
            },
        )

        yield AssistantStreamEvent(
            event_type="done",
            data={
                "session_id": session_id,
                "total_length": len(total_content),
                "duration_ms": elapsed_ms,
                "model_id": config.model_id,
                "kb_datasets_used": config.kb_dataset_ids if retrieved_contexts else [],
                "context_truncated": context_result.truncated_count > 0,
                # Phase 3: RAG quality info
                "rag_quality": rag_evaluation.quality_score if rag_evaluation else None,
                "citations_count": len(citations),
                # Phase 4: Output validation
                "output_warnings": output_warnings,
            },
        )

    async def chat(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
        persist_messages: bool = True,
    ) -> dict[str, Any]:
        """
        Non-streaming chat completion.

        Returns a dict with:
            - content: The assistant's response
            - usage: Token usage
            - contexts: Retrieved KB contexts
            - duration_ms: Total time
        """
        start_time = time.time()
        await self._ensure_session_exists(user=user, session_id=session_id)

        if history is None and self.session_manager:
            try:
                session = await self.session_manager.get(session_id)
                if session and session.history:
                    history = _session_history_to_messages(session.history)
                else:
                    history = []
            except Exception as exc:
                logger.warning(f"Failed to load session history (chat): {exc}")
                history = []
        else:
            history = history or []

        if persist_messages and self.session_manager:
            try:
                await self.session_manager.add_message(
                    session_id=session_id,
                    role="user",
                    content=message,
                    metadata={"timestamp": datetime.utcnow().isoformat()},
                )
            except Exception as exc:
                logger.warning(f"Failed to persist user message (chat): {exc}")

        async def _persist_assistant_chat_message(
            content_text: str,
            contexts: list[dict[str, Any]] | None = None,
        ) -> None:
            if not (persist_messages and self.session_manager):
                return
            try:
                await self.session_manager.add_message(
                    session_id=session_id,
                    role="assistant",
                    content=content_text,
                    metadata={
                        "timestamp": datetime.utcnow().isoformat(),
                        "model_id": config.model_id,
                        "contexts": contexts or [],
                    },
                )
            except Exception as exc:
                logger.warning(f"Failed to persist assistant message (chat): {exc}")

        domain_policy, _ = await self._resolve_domain_policy(user, config.kb_dataset_ids)
        if domain_policy:
            decision = domain_policy.precheck_query(message)
            if decision and decision.action == "decline":
                await _persist_assistant_chat_message(decision.response or "")
                return {
                    "content": decision.response or "",
                    "usage": {},
                    "contexts": [],
                    "duration_ms": (time.time() - start_time) * 1000,
                    "model_id": config.model_id,
                    "run_id": None,
                }

        # Retrieve KB context
        retrieved_contexts: list[RetrievedContext] = []
        if config.kb_mode == RAGMode.AUTO and config.kb_dataset_ids and self.kb_service:
            retrieved_contexts = await self._retrieve_context(
                user=user,
                query=message,
                dataset_ids=config.kb_dataset_ids,
                top_k=config.kb_top_k,
                score_threshold=config.kb_score_threshold,
                include_images=config.kb_include_images,
            )

        if domain_policy:
            ctx_payload = [
                {
                    "dataset_id": ctx.dataset_id,
                    "dataset_name": ctx.dataset_name,
                    "chunks": ctx.chunks,
                }
                for ctx in retrieved_contexts
            ]
            decision = domain_policy.precheck_context(message, ctx_payload)
            if decision and decision.action == "decline":
                await _persist_assistant_chat_message(decision.response or "", contexts=ctx_payload)
                return {
                    "content": decision.response or "",
                    "usage": {},
                    "contexts": ctx_payload,
                    "duration_ms": (time.time() - start_time) * 1000,
                    "model_id": config.model_id,
                    "run_id": None,
                }

        # Build messages
        messages = self._build_messages(
            message=message,
            history=history,
            config=config,
            retrieved_contexts=retrieved_contexts,
            session_id=session_id,
            domain_rules=domain_policy.scenario_rules() if domain_policy else "",
            include_citations=bool(domain_policy),
            authority_sort=False,
        )

        # Get response
        content, usage = await self.model_registry.chat(
            model_id=config.model_id,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        if domain_policy:
            content = domain_policy.sanitize_answer(content)
            issues = domain_policy.validate_answer(content)
            if issues:
                context_text = self._format_context(
                    retrieved_contexts,
                    include_citations=True,
                    authority_sort=False,
                )
                repaired = await self._repair_with_policy(
                    policy=domain_policy,
                    user_message=message,
                    context_text=context_text,
                    answer=content,
                    model_id=config.model_id,
                    temperature=min(config.temperature, 0.3),
                    max_tokens=config.max_tokens,
                    issues=issues,
                )
                repaired = domain_policy.sanitize_answer(repaired)
                if not domain_policy.validate_answer(repaired):
                    content = repaired

        elapsed_ms = (time.time() - start_time) * 1000

        # Record usage to database for billing/analytics
        if usage:
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            try:
                usage_recorder = self.usage_recorder
                await usage_recorder.record_usage(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    model=config.model_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    service_id="__builtin_assistant__",
                    provider=config.model_provider.value,
                    latency_ms=int(elapsed_ms),
                    request_type="chat",
                    metadata={
                        "session_id": session_id,
                        "kb_datasets": config.kb_dataset_ids if retrieved_contexts else [],
                    },
                )
                logger.debug(f"Recorded usage: {usage} for user {user.user_id}")
            except Exception as e:
                logger.warning(f"Failed to record usage: {e}")

            # Update real-time metrics in Redis for dashboard
            try:
                realtime_metrics = self.realtime_metrics
                if realtime_metrics and (input_tokens > 0 or output_tokens > 0):
                    await realtime_metrics.record_token_usage(input_tokens, output_tokens)
                    logger.debug(
                        f"Updated realtime token metrics: input={input_tokens}, output={output_tokens}"
                    )
            except Exception as e:
                logger.warning(f"Failed to update realtime metrics: {e}")

        await _persist_assistant_chat_message(
            content,
            contexts=[
                {
                    "dataset_id": ctx.dataset_id,
                    "dataset_name": ctx.dataset_name,
                }
                for ctx in retrieved_contexts
            ],
        )

        return {
            "content": content,
            "usage": usage,
            "contexts": [
                {
                    "dataset_id": ctx.dataset_id,
                    "dataset_name": ctx.dataset_name,
                    "chunks": ctx.chunks,
                }
                for ctx in retrieved_contexts
            ],
            "duration_ms": elapsed_ms,
            "model_id": config.model_id,
            "run_id": None,
        }

    async def _retrieve_context(
        self,
        user: UserContext,
        query: str,
        dataset_ids: list[str],
        top_k: int,
        score_threshold: float,
        include_images: bool,
    ) -> list[RetrievedContext]:
        """Retrieve context from knowledge bases - PARALLEL retrieval for performance."""
        logger.info(
            f"_retrieve_context called with datasets={dataset_ids}, query='{query[:50]}...'"
        )

        async def retrieve_single_dataset(dataset_id: str) -> RetrievedContext | None:
            """Retrieve from a single dataset - designed for parallel execution."""
            start = time.time()
            logger.info(f"Retrieving from dataset '{dataset_id}'")
            try:
                # Use retrieve_with_images if available and requested
                if include_images and hasattr(self.kb_service, "retrieve_with_images"):
                    results, meta = await self.kb_service.retrieve_with_images(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,
                        include_images=True,
                        image_boost=3.0,
                        use_separate_thresholds=True,
                        image_score_threshold=0.3,
                    )
                else:
                    results, meta = await self.kb_service.retrieve(
                        user=user,
                        dataset_id=dataset_id,
                        query=query,
                        top_k=top_k,
                        score_threshold=score_threshold,
                    )

                took_ms = (time.time() - start) * 1000
                # Debug: Log content types and image_url presence
                content_type_counts = {}
                image_url_count = 0
                for r in results:
                    ct = r.metadata.get("content_type", getattr(r, "content_type", "text"))
                    content_type_counts[ct] = content_type_counts.get(ct, 0) + 1
                    if r.image_url:
                        image_url_count += 1
                logger.info(
                    f"Dataset '{dataset_id}' returned {len(results)} results in {took_ms:.1f}ms - content_types={content_type_counts}, with_image_url={image_url_count}"
                )

                # Convert results to serializable format
                chunks = []
                scores: list[float] = []
                for r in results:
                    with contextlib.suppress(TypeError, ValueError):
                        scores.append(float(r.score))
                    chunk = {
                        "content": r.text,
                        "score": r.score,
                        "metadata": r.metadata or {},
                        "segment_id": r.segment_id,
                        "document_id": r.document_id,
                    }
                    citation_text = (r.metadata or {}).get("citation_text")
                    if citation_text:
                        chunk["citation_text"] = citation_text
                    source_url = (r.metadata or {}).get("source_url") or (r.metadata or {}).get(
                        "source_uri"
                    )
                    if source_url:
                        chunk["source_url"] = source_url
                    if r.image_url:
                        chunk["image_url"] = r.image_url
                    chunks.append(chunk)

                if chunks:
                    avg_score = sum(scores) / len(scores) if scores else 0.0
                    top_score = max(scores) if scores else 0.0
                    return RetrievedContext(
                        dataset_id=dataset_id,
                        dataset_name=meta.get("dataset_name", dataset_id),
                        chunks=chunks,
                        query=query,
                        took_ms=took_ms,
                        avg_score=avg_score,
                        top_score=top_score,
                    )
                return None

            except Exception as e:
                logger.error(f"Failed to retrieve from dataset {dataset_id}: {e}", exc_info=True)
                return None

        # PARALLEL retrieval using asyncio.gather - significant latency improvement
        results = await asyncio.gather(
            *[retrieve_single_dataset(ds_id) for ds_id in dataset_ids], return_exceptions=True
        )

        # Filter out None results and exceptions
        contexts = [r for r in results if r is not None and not isinstance(r, Exception)]

        logger.info(
            f"[KB RETRIEVE] Total: {len(contexts)} contexts with chunks (parallel retrieval)"
        )
        return contexts

    async def _load_user_memory(
        self,
        user: UserContext,
        session_id: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """
        Load user memory/preferences - designed for parallel execution.

        Returns:
            tuple of (user_preferences_text, memory_dict_for_event)
        """
        user_preferences: str | None = None
        memory_data: dict[str, Any] | None = None

        # Use MemoryService if available (preferred)
        if self.memory_service:
            try:
                memories = await self.memory_service.list_user_memories(
                    tenant_id=user.tenant_id, user_id=user.user_id, limit=20
                )
                if memories:
                    # Format as bullet points
                    memory_lines = []
                    for k, v in memories.items():
                        val_str = str(v)
                        if len(val_str) > 500:
                            val_str = val_str[:500] + "..."
                        memory_lines.append(f"- {k}: {val_str}")

                    if memory_lines:
                        user_preferences = "## User Memories (Facts & Preferences)\n\n" + "\n".join(
                            memory_lines
                        )
                        memory_data = memories
                        logger.info(f"Loaded {len(memories)} user memories for {user.user_id}")
            except Exception as e:
                logger.warning(f"Failed to load user memories: {e}")

        # Fallback to legacy MemoryManager
        elif self.db:
            try:
                memory_manager = MemoryManager(
                    db=self.db,
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    session_id=session_id,
                )
                prefs = await memory_manager.get_user_preferences()
                if prefs:
                    pref_lines = []
                    if prefs.get("language"):
                        pref_lines.append(f"- Preferred language: {prefs['language']}")
                    if prefs.get("response_style"):
                        pref_lines.append(f"- Response style: {prefs['response_style']}")
                    if pref_lines:
                        user_preferences = "\n".join(pref_lines)
                        memory_data = prefs
                        logger.info(
                            f"Loaded user preferences for {user.user_id}: {list(prefs.keys())}"
                        )
            except Exception as e:
                logger.warning(f"Failed to load user preferences: {e}")

        return user_preferences, memory_data

    def _get_task_planner(self) -> TaskPlanner:
        """Lazy load task planner."""
        if not self._task_planner:
            # We need a model client for the planner
            # Use default provider (OPENAI) for task planning
            provider = ModelProvider.OPENAI
            model_client = self.model_registry.get_client(provider)
            self._task_planner = create_task_planner(model_client=model_client)
        return self._task_planner

    def _get_tool_orchestrator(self) -> ToolOrchestrator:
        """Lazy load tool orchestrator."""
        if not self._tool_orchestrator:
            from .tools import get_tool_registry

            self._tool_orchestrator = create_tool_orchestrator(
                tool_registry=get_tool_registry(),
                execution_gateway=self.execution_gateway,
            )
        return self._tool_orchestrator

    async def _execute_agent_loop(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Execute using the unified 8-step AgentLoop.

        This is the new enterprise-grade execution path that integrates:
        - ScenarioAwareRetriever for intelligent RAG
        - TaskManager for session isolation
        - ToolInvoker for unified tool execution
        - RAGMetrics for quality tracking

        Args:
            user: User context
            session_id: Session identifier
            message: User's message
            config: Assistant configuration
            history: Optional conversation history

        Yields:
            AssistantStreamEvent objects
        """
        from .agent.agent_loop import AgentLoop, AgentLoopConfig

        # Create AgentLoop configuration. Streaming-first is the only path
        # — the legacy 8-step pipeline was removed.
        loop_config = AgentLoopConfig(
            model_id=config.model_id,
            temperature=config.temperature,
            max_tokens=config.max_tokens or 4096,
            system_prompt=config.system_prompt,
            # Web search preference (True=force, False=AI decides) - passed to prompt
            web_search_enabled=config.web_search_enabled,
            # File attachments (must be processed in AgentLoop streaming-first)
            file_paths=config.file_paths or [],
            # Boundary fields retained for AssistantConfig parity with the frontend;
            # most are no-ops internally now that the legacy 8-step path is gone.
            enable_task_planning=config.enable_task_planning,
            enable_scenario_retrieval=config.use_scenario_retrieval,
            enable_rag_metrics=config.enable_rag_metrics,
            enable_memory_loading=config.enable_memory_loading,
            enable_react_loop=config.enable_react_loop,
            kb_dataset_ids=config.kb_dataset_ids,
            kb_mode=getattr(config.kb_mode, "value", str(config.kb_mode)),
            kb_top_k=config.kb_top_k,
            kb_min_relevance=config.kb_score_threshold,
            max_tool_iterations=5,  # Reasonable limit for tool iterations
            max_concurrent_tools=config.max_parallel_tools,
            execution_profile=config.execution_profile,
            memory_mode=config.memory_mode,
            os_agent_enabled=config.os_agent_enabled,
            openclaw_mode=config.openclaw_mode,
            queue_mode=config.queue_mode,
            context_detail=config.context_detail,
            skills_enabled=config.skills_enabled,
            memory_profile=config.memory_profile,
            # Thinking display: enable for thinking-capable models
            thinking_level=(
                "enabled" if "qwen3" in (config.model_id or "").lower()
                else "high" if "gemini-3" in (config.model_id or "").lower()
                else None
            ),
        )

        logger.info(f"[AGENT LOOP] streaming-first model={loop_config.model_id}")

        # Create AgentLoop instance (system_prompt passed via loop_config)
        from .tool_invoker import create_tool_invoker
        agent_loop = AgentLoop(
            model_registry=self.model_registry,
            kb_service=self.kb_service,
            memory_service=self.memory_service if hasattr(self, "memory_service") else None,
            session_manager=self.session_manager,
            artifact_storage=self.artifact_storage,
            file_processor=self.file_processor,
            execution_gateway=self.execution_gateway,
            request_router=self.request_router,
            database=self.db,
            tool_invoker=create_tool_invoker(
                tenant_tool_policy=self.tenant_tool_policy,
                tenant_mcp_config=self.tenant_mcp_config,
                tool_audit=self.tool_audit,
            ),
        )

        # Load history if not provided
        if history is None and self.session_manager:
            try:
                session = await self.session_manager.get(session_id)
                if session and session.history:
                    history = _session_history_to_messages(session.history)
                else:
                    history = []
            except Exception as e:
                logger.warning(f"Failed to load session history: {e}")
                history = []

        # Execute the agent loop
        async for event in agent_loop.execute(
            session_id=session_id,
            user=user,
            message=message,
            config=loop_config,
            history=history,
        ):
            # Special handling for streaming_first_completed event
            # Split into usage and done events for frontend compatibility
            if event.event_type == "streaming_first_completed":
                # Extract usage data
                usage_data = event.data.get("usage", {}) if isinstance(event.data, dict) else {}
                yield AssistantStreamEvent(
                    event_type="usage",
                    data=usage_data if usage_data else {"input_tokens": 0, "output_tokens": 0},
                )
                # Extract duration and emit done event
                duration_ms = (
                    event.data.get("total_time_ms", 0) if isinstance(event.data, dict) else 0
                )
                content_length = (
                    event.data.get("content_length", 0) if isinstance(event.data, dict) else 0
                )
                yield AssistantStreamEvent(
                    event_type="done",
                    data={
                        "session_id": session_id,
                        "run_id": event.data.get("run_id")
                        if isinstance(event.data, dict)
                        else None,
                        "duration_ms": duration_ms,
                        "total_length": content_length,
                    },
                )
                continue

            # Handle tool_call_started -> tool_call_start (AG-UI compatible)
            if event.event_type == "tool_call_started":
                data = event.data if isinstance(event.data, dict) else {}
                # Parse arguments string to dict for frontend display
                args_str = data.get("arguments", "{}")
                try:
                    import json

                    args_dict = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, TypeError):
                    args_dict = {"raw": args_str} if args_str else {}
                yield AssistantStreamEvent(
                    event_type=StreamEventType.TOOL_CALL_START.value,
                    data={
                        "tool_call_id": data.get("tool_id", ""),
                        "tool_name": data.get("tool_name", ""),
                        "arguments": args_dict,  # Include parsed arguments for card display
                        "step_id": data.get("step_id"),
                        "timestamp": event.timestamp,
                    },
                )
                continue

            # Handle tool_call_completed -> tool_call_end + tool_call_result (AG-UI compatible)
            if event.event_type == "tool_call_completed":
                data = event.data if isinstance(event.data, dict) else {}
                tool_call_id = data.get("tool_id", "")
                tool_name = data.get("tool_name", "")
                metadata = data.get("metadata", {})
                total_results = (
                    metadata.get("total_results") if isinstance(metadata, dict) else None
                )
                duration_ms = data.get("duration_ms")
                if duration_ms is None and isinstance(metadata, dict):
                    duration_ms = metadata.get("duration_ms")
                meta_keys = list(metadata.keys()) if isinstance(metadata, dict) else None
                # Avoid logging large/sensitive metadata payloads (e.g. KB chunks, web snippets).
                logger.info(
                    "[TOOL_CALL_COMPLETED] tool=%s total_results=%s duration_ms=%s metadata_keys=%s",
                    tool_name,
                    total_results,
                    duration_ms,
                    meta_keys,
                )
                # Send tool_call_end event
                yield AssistantStreamEvent(
                    event_type=StreamEventType.TOOL_CALL_END.value,
                    data={
                        "tool_call_id": tool_call_id,
                        "timestamp": event.timestamp,
                    },
                )
                # Send tool_call_result event with metadata for frontend display
                yield AssistantStreamEvent(
                    event_type=StreamEventType.TOOL_CALL_RESULT.value,
                    data={
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                        "result": data.get("result_preview", ""),
                        "success": data.get("success", True),
                        "result_count": metadata.get(
                            "total_results"
                        ),  # For KB/Web search result count
                        "duration_ms": duration_ms,
                        "timestamp": event.timestamp,
                    },
                )
                continue

            # Convert AgentLoopEvent to AssistantStreamEvent
            yield self._convert_agent_loop_event(event)

    def _convert_agent_loop_event(
        self,
        event: AgentLoopEvent,
    ) -> AssistantStreamEvent:
        """Convert AgentLoopEvent to AssistantStreamEvent for compatibility."""

        # Map event types
        event_type_map = {
            "text_delta": StreamEventType.TEXT_DELTA,
            "status": StreamEventType.STATUS,
            "tool_result": StreamEventType.TOOL_CALL_RESULT,
            "retrieval_complete": StreamEventType.CONTEXT_RETRIEVED,
            "rag_evaluation": StreamEventType.RAG_EVALUATION,
            "context_budget": StreamEventType.CONTEXT_BUDGET,
            "context_compacted": StreamEventType.CONTEXT_COMPACTED,
            "context_detail": StreamEventType.CONTEXT_DETAIL,
            "memory_retrieved": StreamEventType.MEMORY_RETRIEVED,
            "memory_reflection_scheduled": StreamEventType.MEMORY_REFLECTION_SCHEDULED,
            "queue_state": StreamEventType.QUEUE_STATE,
            "queue_steered": StreamEventType.QUEUE_STEERED,
            "approval_required": StreamEventType.APPROVAL_REQUIRED,
            "approval_result": StreamEventType.APPROVAL_RESULT,
            "gateway_decision": StreamEventType.GATEWAY_DECISION,
            "skill_selected": StreamEventType.SKILL_SELECTED,
            "skill_loaded": StreamEventType.SKILL_LOADED,
            "skill_create_pending_approval": StreamEventType.SKILL_CREATE_PENDING_APPROVAL,
            "sandbox_decision": StreamEventType.SANDBOX_DECISION,
            "complete": StreamEventType.DONE,
            "error": StreamEventType.ERROR,
            # ReAct thinking events (Phase 3: Agent Intelligence)
            "thinking_delta": StreamEventType.THINKING_DELTA,
            "thinking_start": StreamEventType.THINKING_START,
            "thinking_end": StreamEventType.THINKING_END,
            "thinking_error": StreamEventType.THINKING_ERROR,
        }

        mapped_type = event_type_map.get(event.event_type, event.event_type)

        # Add phase info to data if it's a dict
        data = event.data
        if isinstance(data, dict):
            data = {**data, "agent_loop_phase": event.phase.value}
        elif event.event_type == "text_delta":
            # For text_delta, data is the text content directly
            data = event.data

        return AssistantStreamEvent(
            event_type=mapped_type,
            data=data,
            timestamp=event.timestamp,
        )


    # P2.2: Correction detection patterns
    _CORRECTION_RE = __import__("re").compile(
        r"你搞错了|不对|错了|有问题|你的.*有误|搞混了|弄反了|"
        r"今天是\d{4}年|现在是\d{4}|"
        r"that'?s wrong|incorrect|you'?re wrong|not right|that'?s not|"
        r"no[,.]?\s*(it'?s|the|actually)|wrong answer|fix this|try again",
        __import__("re").IGNORECASE,
    )

    def _detect_user_correction(self, message: str) -> bool:
        """Detect if the user is correcting a previous AI response."""
        return bool(self._CORRECTION_RE.search(message))

    def _is_document_generation_task(self, message: str) -> bool:
        """
        Detect if the user message requests document generation.

        Used for ReAct phase routing to apply two-stage document flow.

        Args:
            message: User's message

        Returns:
            True if this appears to be a document generation request
        """
        doc_keywords = [
            # Chinese keywords
            "写",
            "撰写",
            "生成文档",
            "写报告",
            "写计划",
            "写文章",
            "帮我写",
            "写一个",
            "写一份",
            "文档",
            "报告",
            "计划书",
            "研究报告",
            "分析报告",
            "总结",
            "论文",
            "方案",
            # English keywords
            "write",
            "generate",
            "create",
            "document",
            "report",
            "plan",
            "docx",
            "pdf",
            "markdown",
            "article",
            "paper",
            "summary",
        ]
        message_lower = message.lower()
        return any(kw in message_lower for kw in doc_keywords)

    def _build_scenario_prompt(self, scenario: ScenarioDetectionResult) -> str:
        """Build expert analysis prompt based on detected scenario.

        This method generates scenario-specific prompts that guide the AI to provide
        expert-level, multi-dimensional analysis - a key feature for "Manus-like" capabilities.

        Args:
            scenario: The detected scenario with type and metadata.

        Returns:
            Expert analysis prompt string to inject into system prompt.
        """
        from .prompts.scenario_analysis_prompts import EXPERT_TEMPLATES, SCENARIO_TYPES

        scenario_type = scenario.primary_scenario.value
        scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES.get("general_inquiry", {}))

        scenario_name = scenario_info.get("name", "通用")
        dimensions = scenario_info.get("analysis_dimensions", [])
        expert_template = EXPERT_TEMPLATES.get(
            scenario_type, EXPERT_TEMPLATES.get("general_inquiry", "")
        )

        # Build the expert analysis prompt
        prompt_parts = [
            f"## 专家分析模式 - {scenario_name}",
            "",
            "你现在是一位经验丰富的专家助手。请按照以下框架进行专业分析和回答：",
            "",
            "### 分析维度",
        ]

        for dim in dimensions:
            prompt_parts.append(f"- {dim}")

        prompt_parts.extend(
            [
                "",
                "### 回答框架",
                expert_template,
                "",
                "### 回答要求",
                "1. **准确诊断**：准确识别问题的本质和根源",
                "2. **方案实用**：提供具体可操作的建议",
                "3. **表达专业**：使用恰当的专业术语",
                "4. **逻辑清晰**：层次分明，条理清楚",
                "5. **考虑周全**：涵盖边界情况和注意事项",
            ]
        )

        # Add urgency hint if urgent
        if scenario.urgency.value == "urgent":
            prompt_parts.extend(
                [
                    "",
                    "**注意**：用户的问题标记为紧急，请优先给出最关键的解决步骤。",
                ]
            )

        return "\n".join(prompt_parts)

    def _build_document_analysis_prompt(self, processed_files: ProcessedFiles) -> str:
        """Build expert document analysis prompt based on document structure.

        This method generates prompts that guide the AI to provide deep document
        analysis - a key feature for "Manus-like" capabilities.

        Args:
            processed_files: The processed files with document structure analysis.

        Returns:
            Document analysis prompt string to inject into system prompt.
        """
        structure = processed_files.document_structure

        prompt_parts = [
            "## 文档分析模式",
            "",
            "用户上传了文档供你分析。请使用专业的分析框架进行深度理解和回答。",
        ]

        # Add structure information if available
        if structure:
            prompt_parts.extend(
                [
                    "",
                    "### 文档结构概览",
                    f"- 总字符数：{structure.total_chars:,}",
                    f"- 预计阅读时间：{structure.estimated_reading_time_min} 分钟",
                ]
            )

            # Add section outline if available
            if structure.sections:
                prompt_parts.append(f"- 章节数量：{len(structure.sections)}")
                if structure.key_topics:
                    topics_str = "、".join(structure.key_topics[:5])
                    prompt_parts.append(f"- 主要主题：{topics_str}")

            # Add content characteristics
            characteristics = []
            if structure.has_headers:
                characteristics.append("结构化标题")
            if structure.has_lists:
                characteristics.append("列表内容")
            if structure.has_tables:
                characteristics.append("表格数据")
            if structure.has_code_blocks:
                characteristics.append("代码片段")

            if characteristics:
                prompt_parts.append(f"- 内容特点：{', '.join(characteristics)}")

        # Add analysis framework
        prompt_parts.extend(
            [
                "",
                "### 分析框架",
                "根据用户问题，从以下维度进行深度分析：",
                "",
                "1. **文档概览**：类型、主题、主要内容",
                "2. **核心内容**：关键信息、主要观点、重要数据",
                "3. **结构分析**：文档组织、逻辑脉络",
                "4. **深度洞察**：隐含信息、潜在价值",
                "5. **应用建议**：基于文档内容的行动建议",
                "",
                "### 回答要求",
                "- 准确引用文档中的具体内容",
                "- 对数据和信息进行解读，不只是复述",
                "- 如果文档未涵盖某方面，明确指出",
                "- 提供结构化的分析，便于理解",
            ]
        )

        return "\n".join(prompt_parts)

    def _build_messages(
        self,
        message: str,
        history: list[dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
        processed_files: ProcessedFiles | None = None,
        model_supports_vision: bool = False,
        session_id: str | None = None,
        user_preferences: str | None = None,
        scenario_detection: ScenarioDetectionResult | None = None,
        domain_rules: str = "",
        include_citations: bool = False,
        authority_sort: bool = False,
    ) -> list[ChatMessage]:
        """Build the message list for the model.

        Args:
            message: The user's message text.
            history: Previous conversation history.
            config: Assistant configuration.
            retrieved_contexts: KB retrieval results.
            web_search_context: Web search results as formatted text.
            processed_files: Processed file contents (images, text, descriptions).
            model_supports_vision: Whether the model supports vision/multimodal input.
            session_id: Session ID for working memory lookup (Context Engine mode).
            user_preferences: User preferences loaded from MemoryManager (formatted string).
            scenario_detection: Detected scenario for expert-level analysis prompts.

        Returns:
            List of ChatMessage objects ready to send to the model.
        """
        # Use Context Engine for optimized caching if enabled
        if config.use_context_engine:
            return self._build_messages_with_context_engine(
                message=message,
                history=history,
                config=config,
                retrieved_contexts=retrieved_contexts,
                web_search_context=web_search_context,
                processed_files=processed_files,
                model_supports_vision=model_supports_vision,
                session_id=session_id,
                user_preferences=user_preferences,
                domain_rules=domain_rules,
                include_citations=include_citations,
                authority_sort=authority_sort,
            )

        # Legacy message building (original implementation) - Now with Manus-style prompts
        messages: list[ChatMessage] = []

        # System prompt - Use Manus-style modular prompt builder
        if config.system_prompt:
            # User provided custom system prompt, use it directly
            system_content = config.system_prompt
            if domain_rules:
                system_content = f"{system_content}\n\n{domain_rules}"
            logger.info("[SYSTEM PROMPT] Using custom system prompt from config")
        else:
            # Build Manus-style system prompt with scenario rules
            # Only inject scenario framework when agent_loop is enabled (complex tasks)
            # For simple queries, skip scenario injection to follow "minimal effective context" principle
            scenario_rules = ""
            if (
                config.use_agent_loop
                and scenario_detection
                and scenario_detection.confidence >= 0.6
            ):
                scenario_rules = self._build_scenario_prompt(scenario_detection)
                logger.info(
                    f"[SCENARIO INJECT] Building prompt with scenario: {scenario_detection.primary_scenario.value}"
                )
            elif scenario_detection:
                logger.info(
                    f"[SCENARIO SKIP] Skipping scenario injection (agent_loop={config.use_agent_loop}, confidence={scenario_detection.confidence:.2f})"
                )

            if domain_rules:
                scenario_rules = f"{scenario_rules}\n{domain_rules}".strip()

            # Get dataset names for display
            dataset_names = None
            if config.kb_dataset_ids:
                dataset_names = config.kb_dataset_ids

            system_content = self.build_default_system_prompt(
                user_role="user",  # Could be enhanced with actual user role
                available_datasets=dataset_names,
                scenario_rules=scenario_rules,
            )
            logger.info("[SYSTEM PROMPT] Built Manus-style modular prompt")

        # Inject user preferences using new modular function
        if user_preferences:
            system_content = inject_user_preferences(system_content, user_preferences)
            logger.info(
                f"[MEMORY INJECT] Injected user preferences, length: {len(user_preferences)}"
            )

        # Inject KB context using new modular function
        if retrieved_contexts:
            context_text = self._format_context(
                retrieved_contexts,
                include_citations=include_citations,
                authority_sort=authority_sort,
            )
            logger.info(
                f"[KB INJECT] Injecting context from {len(retrieved_contexts)} datasets, text length: {len(context_text)}"
            )
            logger.debug(f"[KB INJECT] Context preview: {context_text[:500]}...")
            system_content = inject_kb_context(system_content, context_text)
        else:
            logger.info("[KB INJECT] No retrieved_contexts to inject")

        # Inject web search context using new modular function
        if web_search_context:
            system_content = inject_web_context(system_content, web_search_context)

        # Inject document analysis prompt if document content is present
        # This enables deep, expert-level document analysis - a key "Manus-like" feature
        if processed_files and processed_files.text_content:
            # Build document structure info
            structure_info = ""
            if (
                hasattr(processed_files, "document_structure")
                and processed_files.document_structure
            ):
                struct = processed_files.document_structure
                structure_info = f"总字符数: {struct.total_chars}, 总行数: {struct.total_lines}"
                if struct.sections:
                    structure_info += f", 章节数: {len(struct.sections)}"

            system_content = inject_document_context(
                system_content,
                content=processed_files.text_content,
                structure_info=structure_info,
            )
            logger.info("[DOC INJECT] Injected document context with Manus-style template")

        messages.append(ChatMessage(role="system", content=system_content))
        logger.info(f"[SYSTEM PROMPT] Total length: {len(system_content)} chars")
        if len(system_content) > 1000:
            logger.debug(f"[SYSTEM PROMPT] First 500 chars: {system_content[:500]}...")
            logger.debug(f"[SYSTEM PROMPT] Last 500 chars: ...{system_content[-500:]}")

        # History
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append(ChatMessage(role=role, content=content))

        # Build user message with potential file content
        final_message = message
        user_images: list[str] | None = None

        if processed_files:
            if model_supports_vision and processed_files.has_images:
                # Vision model: pass images as base64 data URLs
                # ChatMessage.images field will be converted to OpenAI Vision API format
                # in _build_openai_body (already handles data URL format)
                user_images = []

                # Add regular images
                for img in processed_files.images:
                    user_images.append(f"data:{img.media_type};base64,{img.base64_data}")

                # Add PDF page images (converted from PDF)
                for pdf_page in processed_files.pdf_pages:
                    user_images.append(f"data:{pdf_page.media_type};base64,{pdf_page.base64_data}")

                logger.info(
                    f"[FILE INJECT] Added {len(processed_files.images)} images + "
                    f"{len(processed_files.pdf_pages)} PDF pages for vision model"
                )

            # For text-only models OR additional text content from documents
            # Inject text content and image descriptions into the user message
            if processed_files.text_content:
                final_message += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
                logger.info(
                    f"[FILE INJECT] Added text content: {len(processed_files.text_content)} chars"
                )

            if processed_files.image_descriptions and not model_supports_vision:
                # Only add image descriptions for text-only models
                # Vision models can see the images directly
                descriptions = "\n".join(
                    f"- 图像 {i + 1}: {desc}"
                    for i, desc in enumerate(processed_files.image_descriptions)
                )
                final_message += f"\n\n---\n[图像描述]\n{descriptions}"
                logger.info(
                    f"[FILE INJECT] Added {len(processed_files.image_descriptions)} image descriptions for text model"
                )

            # Log warning if files were processed but no content was extracted
            if not processed_files.text_content and not processed_files.has_images:
                logger.warning(
                    f"[FILE INJECT] No content extracted from files. "
                    f"requires_rag={processed_files.requires_rag}, "
                    f"file_metadata={processed_files.file_metadata}"
                )

        # Current message (with potential file content and images)
        messages.append(ChatMessage(role="user", content=final_message, images=user_images))

        return messages

    def _build_messages_with_context_engine(
        self,
        message: str,
        history: list[dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
        processed_files: ProcessedFiles | None = None,
        model_supports_vision: bool = False,
        session_id: str | None = None,
        user_preferences: str | None = None,
        domain_rules: str = "",
        include_citations: bool = False,
        authority_sort: bool = False,
    ) -> list[ChatMessage]:
        """Build messages using Context Engine for KV-Cache optimization.

        This method uses the ContextEngine class to construct messages with
        a stable prefix design that maximizes cache hit rates.

        Key differences from legacy _build_messages:
        - System prompt is built with layered structure (stable first)
        - User preferences and long-term memory are injected into system prompt
        - Working memory (task state) is included for multi-step task focus
        - KB/web context goes into current_context (end of user message)

        Args:
            message: The user's message text.
            history: Previous conversation history.
            config: Assistant configuration.
            retrieved_contexts: KB retrieval results.
            web_search_context: Web search results as formatted text.
            processed_files: Processed file contents.
            model_supports_vision: Whether the model supports vision.
            session_id: Session ID for working memory lookup.
            user_preferences: User preferences loaded from MemoryManager (formatted string).

        Returns:
            List of ChatMessage objects with optimized structure.
        """
        # Get provider from model_id to configure ContextEngine
        provider = self._get_provider_from_model(config.model_id)
        context_engine = ContextEngine(provider=provider)

        # Build current context (KB + web search results)
        current_context_parts: list[str] = []
        if retrieved_contexts:
            context_text = self._format_context(
                retrieved_contexts,
                include_citations=include_citations,
                authority_sort=authority_sort,
            )
            current_context_parts.append(self.CONTEXT_TEMPLATE.format(context=context_text))
            logger.info(f"[CONTEXT ENGINE] KB context: {len(context_text)} chars")

        if web_search_context:
            current_context_parts.append(
                self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context)
            )
            logger.info(f"[CONTEXT ENGINE] Web context: {len(web_search_context)} chars")

        # Get working memory task state if available
        task_state: str | None = None
        if session_id and session_id in self._working_memories:
            working_memory = self._working_memories[session_id]
            task_state = working_memory.to_markdown()
            logger.info(f"[CONTEXT ENGINE] Task state injected: {len(task_state)} chars")

        # Determine user_preferences: prefer loaded preferences from MemoryManager,
        # fallback to config.user_preferences
        effective_user_preferences = user_preferences or config.user_preferences
        if effective_user_preferences:
            logger.info(
                f"[CONTEXT ENGINE] User preferences: {len(effective_user_preferences)} chars"
            )

        # Build ContextStructure with layered content
        # Use TTFT-optimized prompt when context engine is enabled (no timestamps!)
        effective_system_prompt = config.system_prompt
        if not effective_system_prompt:
            # Use TTFT-optimized prompt for KV-Cache stability
            effective_system_prompt = get_ttft_optimized_prompt(
                user_role="user",
                available_datasets=config.kb_dataset_ids,
                scenario_rules=domain_rules,
            )
            logger.info("[CONTEXT ENGINE] Built TTFT-optimized system prompt (no timestamps)")
        elif domain_rules:
            effective_system_prompt = f"{effective_system_prompt}\n\n{domain_rules}"

        context_structure = ContextStructure(
            system_prompt=effective_system_prompt,
            tool_definitions=[],  # Tool definitions handled separately
            user_preferences=effective_user_preferences,
            long_term_memory=config.long_term_memory,
            task_state=task_state,
            conversation_history=[
                {"role": h.get("role", "user"), "content": h.get("content", "")}
                for h in history
                if h.get("role") in ("user", "assistant") and h.get("content")
            ],
            current_context="\n\n".join(current_context_parts) if current_context_parts else None,
            current_query=message,
        )

        # Build messages using ContextEngine
        raw_messages = context_engine.build_messages(context_structure)

        # Convert to ChatMessage objects and handle file content
        messages: list[ChatMessage] = []
        for i, msg in enumerate(raw_messages):
            role = msg["role"]
            content = msg["content"]

            # For the last user message, handle file attachments
            if i == len(raw_messages) - 1 and role == "user" and processed_files:
                content, images = self._inject_file_content(
                    content=content,
                    processed_files=processed_files,
                    model_supports_vision=model_supports_vision,
                )
                messages.append(ChatMessage(role=role, content=content, images=images))
            else:
                messages.append(ChatMessage(role=role, content=content))

        logger.info(f"[CONTEXT ENGINE] Built {len(messages)} messages with stable prefix design")
        return messages

    def _inject_file_content(
        self,
        content: str,
        processed_files: ProcessedFiles,
        model_supports_vision: bool,
    ) -> tuple[str, list[str] | None]:
        """Inject file content into user message.

        Args:
            content: Original user message content.
            processed_files: Processed file contents.
            model_supports_vision: Whether model supports vision.

        Returns:
            Tuple of (updated content, optional image list).
        """
        user_images: list[str] | None = None

        if model_supports_vision and processed_files.has_images:
            user_images = []

            # Add regular images
            for img in processed_files.images:
                user_images.append(f"data:{img.media_type};base64,{img.base64_data}")

            # Add PDF page images (converted from PDF)
            for pdf_page in processed_files.pdf_pages:
                user_images.append(f"data:{pdf_page.media_type};base64,{pdf_page.base64_data}")

            logger.info(
                f"[CONTEXT ENGINE] Added {len(processed_files.images)} images + "
                f"{len(processed_files.pdf_pages)} PDF pages"
            )

        if processed_files.text_content:
            content += f"\n\n---\n[上传文件内容]\n{processed_files.text_content}"
            logger.info(
                f"[CONTEXT ENGINE] Added text content: {len(processed_files.text_content)} chars"
            )

        if processed_files.image_descriptions and not model_supports_vision:
            descriptions = "\n".join(
                f"- 图像 {i + 1}: {desc}"
                for i, desc in enumerate(processed_files.image_descriptions)
            )
            content += f"\n\n---\n[图像描述]\n{descriptions}"
            logger.info(
                f"[CONTEXT ENGINE] Added {len(processed_files.image_descriptions)} image descriptions"
            )

        return content, user_images

    def get_working_memory(self, session_id: str) -> WorkingMemory:
        """Get or create working memory for a session.

        Args:
            session_id: The session ID.

        Returns:
            WorkingMemory instance for the session.
        """
        if session_id not in self._working_memories:
            self._working_memories[session_id] = WorkingMemory(session_id=session_id)
        return self._working_memories[session_id]

    def clear_working_memory(self, session_id: str) -> None:
        """Clear working memory for a session.

        Args:
            session_id: The session ID.
        """
        if session_id in self._working_memories:
            del self._working_memories[session_id]

    @property
    def task_planner(self) -> TaskPlanner:
        """Get or create the task planner instance.

        Returns:
            TaskPlanner instance for task decomposition.
        """
        if self._task_planner is None:
            self._task_planner = TaskPlanner()
        return self._task_planner

    def get_tool_orchestrator(self, max_parallel: int = 5) -> ToolOrchestrator:
        """Get or create a tool orchestrator instance.

        Args:
            max_parallel: Maximum number of parallel tool executions.

        Returns:
            ToolOrchestrator instance for parallel tool execution.
        """
        if self._tool_orchestrator is None:
            from .tools import get_tool_registry

            registry = get_tool_registry()
            self._tool_orchestrator = ToolOrchestrator(
                tool_registry=registry,
                max_parallel=max_parallel,
                execution_gateway=self.execution_gateway,
            )
        return self._tool_orchestrator

    async def _execute_with_planning(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
        history: list[dict[str, str]],
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Execute a complex request using task planning and parallel tool execution.

        This method implements Phase 2.4 of the Enterprise Assistant Optimization:
        1. Creates an execution plan using TaskPlanner
        2. Sets up WorkingMemory with goal and tasks
        3. Uses ToolOrchestrator to execute the plan in parallel groups
        4. Yields progress events (TASK_PLANNING, WORKING_MEMORY_UPDATE)
        5. Collects results for final response generation

        Args:
            user: User context for authentication/authorization
            session_id: Session ID for conversation tracking
            message: User's message (the request to plan and execute)
            config: Assistant configuration
            history: Processed conversation history
            retrieved_contexts: KB retrieval results
            web_search_context: Web search results

        Yields:
            AssistantStreamEvent objects for planning progress and results
        """
        logger.info(f"[TASK PLANNING] Starting planning mode for session {session_id}")

        # Get or create working memory for this session
        working_memory = self.get_working_memory(session_id)
        working_memory.clear()  # Clear any previous state

        # Get available tools from registry
        from .tools import get_tool_registry

        registry = get_tool_registry()
        available_tools = [tool.name for tool in registry.list_tools(user=user)]

        # Add KB retrieval tool if KB service is available
        if self.kb_service and config.kb_dataset_ids and "kb_search" not in available_tools:
            available_tools.append("kb_search")

        # Add web search tool if enabled
        if (
            config.web_search_enabled
            and self.tavily_tool.is_configured
            and "web_search" not in available_tools
        ):
            available_tools.append("web_search")

        logger.info(f"[TASK PLANNING] Available tools: {available_tools}")

        # Step 1: Create execution plan using office templates or TaskPlanner
        try:
            from .office.planner import build_plan_for_scenario
            from .office.scenario import detect_scenario

            scenario = detect_scenario(message)
            plan = build_plan_for_scenario(scenario, message)
            plan_from_template = plan is not None
            if plan is None:
                plan = await self.task_planner.create_plan(
                    user_request=message,
                    available_tools=available_tools,
                    context={
                        "session_id": session_id,
                        "has_kb_context": len(retrieved_contexts) > 0,
                        "has_web_context": web_search_context is not None,
                    },
                    use_llm=False,  # Use rule-based planning for now
                )
                plan_from_template = False

            # Yield TASK_PLANNING event with plan details
            yield AssistantStreamEvent(
                event_type=StreamEventType.TASK_PLANNING.value,
                data={
                    "goal": plan.goal,
                    "tasks": [task.to_dict() for task in plan.tasks],
                    "parallel_groups": plan.parallel_groups,
                    "metadata": plan.metadata,
                    "estimated_duration_ms": plan.get_total_estimated_duration(),
                },
            )

            logger.info(
                f"[TASK PLANNING] Created plan with {len(plan.tasks)} tasks "
                f"in {len(plan.parallel_groups)} parallel groups"
            )

        except Exception as e:
            logger.error(f"[TASK PLANNING] Failed to create plan: {e}")
            yield AssistantStreamEvent(
                event_type=StreamEventType.ERROR.value,
                data={"message": f"Task planning failed: {str(e)}", "recoverable": True},
            )
            return

        # Confirmation gate for template plans (when user wants explicit confirmation)
        if plan_from_template and config.confirm_plan:
            if self.memory_service:
                try:
                    await self.memory_service.set_session_memory(
                        tenant_id=user.tenant_id,
                        session_id=session_id,
                        key="pending_plan",
                        value=plan.to_dict(),
                        metadata={"scenario": scenario.value},
                    )
                except Exception as e:
                    logger.warning(f"Failed to store pending plan: {e}")
            elif self.db:
                try:
                    memory_manager = MemoryManager(
                        db=self.db,
                        tenant_id=user.tenant_id,
                        user_id=user.user_id,
                        session_id=session_id,
                    )
                    await memory_manager.remember(
                        key="pending_plan",
                        value=plan.to_dict(),
                        layer="session",
                        metadata={"scenario": scenario.value},
                    )
                except Exception as e:
                    logger.warning(f"Failed to store pending plan in legacy memory: {e}")

            yield AssistantStreamEvent(
                event_type=StreamEventType.STATUS.value,
                data={
                    "status": "plan_ready",
                    "message": "Plan ready. Please confirm to execute.",
                    "requires_confirmation": True,
                    "plan": plan.to_dict(),
                },
            )
            return

        # Step 2: Set up WorkingMemory with goal and tasks
        working_memory.set_goal(plan.goal)
        for task in plan.tasks:
            working_memory.add_task(task.id, task.description)

        # Yield initial working memory state
        yield AssistantStreamEvent(
            event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
            data={
                "session_id": session_id,
                "goal": working_memory.goal,
                "tasks": [t.to_dict() for t in working_memory.tasks],
                "progress": working_memory.get_progress(),
            },
        )

        # Step 3: Execute plan using ToolOrchestrator
        orchestrator = self.get_tool_orchestrator(max_parallel=config.max_parallel_tools)
        collected_results: list[ToolExecutionResult] = []
        invocation_context = ToolInvocationContext(
            session_id=session_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            request_id=f"planning:{session_id}:{int(time.time() * 1000)}",
            run_id=str(uuid.uuid4()),
            scope_id=session_id,
            policy_profile=config.execution_profile,
            os_agent_enabled=config.os_agent_enabled,
            kb_dataset_ids=config.kb_dataset_ids or [],
            user=user,
        )

        try:
            async for result in orchestrator.execute_plan(
                plan,
                working_memory,
                invocation_context=invocation_context,
            ):
                # Store result for final response generation
                collected_results.append(result)

                # Yield working memory update for each task completion
                yield AssistantStreamEvent(
                    event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
                    data={
                        "session_id": session_id,
                        "goal": working_memory.goal,
                        "tasks": [t.to_dict() for t in working_memory.tasks],
                        "progress": working_memory.get_progress(),
                        "last_completed_task": {
                            "task_id": result.task_id,
                            "tool": result.tool,
                            "success": result.success,
                            "duration_ms": result.duration_ms,
                            "error": result.error,
                        },
                    },
                )

                # Also yield tool result event for frontend visualization
                yield AssistantStreamEvent(
                    event_type=StreamEventType.TOOL_RESULT.value,
                    data={
                        "tool_call_id": result.task_id,
                        "tool_name": result.tool,
                        "success": result.success,
                        "result": str(result.result)[:1000] if result.result else None,
                        "error": result.error,
                        "duration_ms": result.duration_ms,
                    },
                )

                logger.info(
                    f"[TASK PLANNING] Task {result.task_id} completed: "
                    f"success={result.success}, duration={result.duration_ms:.1f}ms"
                )

        except Exception as e:
            logger.error(f"[TASK PLANNING] Execution failed: {e}")
            yield AssistantStreamEvent(
                event_type=StreamEventType.ERROR.value,
                data={"message": f"Task execution failed: {str(e)}", "recoverable": True},
            )

        # Step 4: Generate final response using collected results
        # Build a context message with all collected results
        results_context = self._format_execution_results(collected_results)
        if results_context:
            working_memory.add_info(
                key="execution_results", value=results_context, source="tool_orchestrator"
            )

        # Store collected results in working memory for downstream use
        for result in collected_results:
            if result.success and result.result:
                working_memory.add_info(
                    key=f"result_{result.task_id}",
                    value=str(result.result)[:500],
                    source=result.tool,
                )

        # Final working memory state
        yield AssistantStreamEvent(
            event_type=StreamEventType.WORKING_MEMORY_UPDATE.value,
            data={
                "session_id": session_id,
                "goal": working_memory.goal,
                "tasks": [t.to_dict() for t in working_memory.tasks],
                "progress": working_memory.get_progress(),
                "collected_info": [info.to_dict() for info in working_memory.collected_info],
                "complete": True,
            },
        )

        logger.info(f"[TASK PLANNING] Execution complete: {working_memory.get_progress()}")

    def _format_execution_results(self, results: list[ToolExecutionResult]) -> str:
        """Format tool execution results for context injection.

        Args:
            results: List of tool execution results

        Returns:
            Formatted string summarizing execution results
        """
        if not results:
            return ""

        parts = ["## Task Execution Results\n"]
        for result in results:
            status = "SUCCESS" if result.success else "FAILED"
            parts.append(f"### {result.task_id} ({result.tool}) - {status}")

            if result.success and result.result:
                # Truncate long results
                result_str = str(result.result)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "..."
                parts.append(f"Result: {result_str}")
            elif result.error:
                parts.append(f"Error: {result.error}")

            parts.append(f"Duration: {result.duration_ms:.1f}ms\n")

        return "\n".join(parts)

    @staticmethod
    def _validate_citations(answer: str, citations: list[Citation]) -> list[str]:
        """Validate that citations are present when RAG is used."""
        if answer and not citations:
            return ["Missing citations for RAG response."]
        return []

    def _format_context(
        self,
        contexts: list[RetrievedContext],
        max_content_length: int = 400,  # TTFT optimization: truncate long chunks
        include_citations: bool = False,
        authority_sort: bool = False,
    ) -> str:
        """
        Format retrieved contexts for injection into the prompt.

        Args:
            contexts: List of retrieved context objects
            max_content_length: Maximum characters per chunk (default 400 for TTFT optimization)

        Returns:
            Formatted context string
        """
        parts = []
        for ctx in contexts:
            parts.append(f"### From: {ctx.dataset_name}")
            chunks = list(ctx.chunks)
            if authority_sort:
                try:
                    from src.services.knowledge.islamic_metadata import get_authority_order

                    def _authority_key(ch: dict[str, Any]) -> int:
                        meta = ch.get("metadata") or {}
                        source_type = (
                            meta.get("source_type") or meta.get("islamic_source_type") or "unknown"
                        )
                        return get_authority_order(str(source_type))

                    chunks = sorted(chunks, key=_authority_key)
                except Exception as exc:
                    logger.debug(f"Authority sort skipped: {exc}")

            for i, chunk in enumerate(chunks, 1):
                content = chunk["content"]
                score = chunk.get("score", 0)
                source = chunk.get("source_url", "")
                citation_text = None
                if include_citations:
                    meta = chunk.get("metadata") or {}
                    citation_text = meta.get("citation_text") or chunk.get("citation_text")

                # TTFT optimization: truncate long content to reduce tokens
                if len(content) > max_content_length:
                    content = content[:max_content_length] + "..."

                header = f"\n[{i}] (relevance: {score:.2f})"
                if source:
                    header += f" [Source: {source}]"
                if citation_text:
                    header += f" [Citation: {citation_text}]"
                parts.append(f"{header}\n{content}")

        return "\n".join(parts)

    def _create_tool_error(
        self,
        tool_name: str,
        tool_call_id: str,
        error: Exception,
        arguments: dict[str, Any],
    ) -> ToolErrorInfo:
        """
        Create a structured ToolErrorInfo from an exception.

        This implements the Manus Context Engineering principle of preserving
        error information for the agent. The suggestion field provides
        actionable guidance based on common error patterns.

        Args:
            tool_name: Name of the tool that failed
            tool_call_id: ID of the tool call
            error: The exception that was raised
            arguments: Arguments that were passed to the tool

        Returns:
            ToolErrorInfo with rich error context
        """
        error_type = type(error).__name__
        error_message = str(error)

        # Generate suggestions based on common error patterns
        suggestion = self._get_error_suggestion(tool_name, error_type, error_message, arguments)

        return ToolErrorInfo(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            error_type=error_type,
            error_message=error_message,
            arguments=arguments,
            suggestion=suggestion,
        )

    def _get_error_suggestion(
        self,
        tool_name: str,
        error_type: str,
        error_message: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """
        Generate a suggestion for recovering from a tool error.

        Maps common error patterns to actionable suggestions that help
        the model adjust its approach on retry.

        Args:
            tool_name: Name of the tool
            error_type: Type of the error
            error_message: Error message
            arguments: Tool arguments

        Returns:
            A suggestion string, or None if no specific suggestion applies
        """
        error_lower = error_message.lower()

        # Code execution errors
        if tool_name == "execute_python_code":
            if "timeout" in error_lower:
                return "The code took too long. Consider breaking it into smaller steps or optimizing the algorithm."
            if "syntax" in error_lower:
                return "There's a syntax error in the code. Check for missing colons, brackets, or indentation issues."
            if "import" in error_lower or "module" in error_lower:
                return "A required module is not available. Use only standard library modules or check module name spelling."
            if "memory" in error_lower:
                return "The code used too much memory. Consider processing data in smaller chunks."
            if "permission" in error_lower or "access" in error_lower:
                return "File access was denied. The sandbox restricts file system access."

        # Image generation errors
        if tool_name == "generate_image":
            if "content policy" in error_lower or "safety" in error_lower:
                return "The prompt was flagged by content policy. Rephrase the prompt to be more appropriate."
            if "rate limit" in error_lower:
                return "Rate limit exceeded. Wait a moment before trying again."
            if "invalid" in error_lower and "prompt" in error_lower:
                return "The prompt format is invalid. Ensure it's a clear, descriptive text."

        # Document generation errors
        if tool_name == "generate_document":
            if "format" in error_lower:
                return "The document format is not supported. Use docx, pdf, or md."
            if "content" in error_lower and "empty" in error_lower:
                return (
                    "Document content cannot be empty. Provide content to include in the document."
                )

        # JSON parsing errors (common across tools)
        if error_type == "JSONDecodeError":
            return "The arguments contain invalid JSON. Ensure proper JSON formatting with quoted strings and escaped characters."

        # Network/API errors
        if "connection" in error_lower or "network" in error_lower:
            return "Network connection failed. This may be temporary - you can retry."
        if "api" in error_lower and ("key" in error_lower or "auth" in error_lower):
            return "API authentication failed. This is a configuration issue, not something you can fix."

        # Generic timeout
        if "timeout" in error_lower:
            return "The operation timed out. Consider simplifying the request or breaking it into smaller parts."

        return None

    def get_available_models(self) -> list[dict[str, Any]]:
        """Get list of available models with metadata."""
        models = self.model_registry.get_available_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider.value,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "supports_vision": m.supports_vision,
                "supports_tools": m.supports_tools,
            }
            for m in models
        ]

    def get_gateway_policies(self) -> dict[str, Any]:
        """Return assistant gateway policy snapshot for API exposure."""
        if not self.execution_gateway:
            return {}
        return self.execution_gateway.get_policies()

    async def approve_tool_request(
        self,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        approved: bool,
        approver_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Approve or reject a pending tool invocation."""
        if not self.execution_gateway:
            return None
        return await self.execution_gateway.approve(
            approval_id=approval_id,
            tenant_id=tenant_id,
            user_id=user_id,
            approved=approved,
            approver_user_id=approver_user_id,
            reason=reason,
        )

    async def get_run_status(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Fetch run status for the current user/tenant."""
        if not self.execution_gateway:
            return None
        return await self.execution_gateway.get_run(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )

    async def close(self) -> None:
        """Cleanup resources."""
        await self.model_registry.close()

    def _register_code_executor_tool(self) -> None:
        """Register the code executor tool if available."""
        if not self.code_executor:
            return

        from .tools import get_tool_registry

        registry = get_tool_registry()
        executor = CodeExecutorToolExecutor(code_executor=self.code_executor)
        registry.register(CODE_EXECUTOR_TOOL, executor)
        logger.info("Registered code executor tool")

    def _get_provider_from_model(self, model_id: str) -> str:
        """Get provider identifier from model ID for cache metrics."""
        model_info = self.model_registry.get_model(model_id)
        if model_info:
            provider = model_info.provider.value.lower()
            if "google" in provider or "gemini" in provider:
                return "gemini"
            elif "dashscope" in provider or "qwen" in provider:
                return "dashscope"
        return "dashscope"  # Default fallback

    async def _prepare_code_execution_files(
        self,
        file_paths: list[str] | None,
        retrieved_contexts: list[RetrievedContext] | None,
    ) -> tuple[list[InputFile] | None, list[KBDocument] | None]:
        """
        Prepare files for code execution in Docker container.

        Converts uploaded files and KB contexts into formats accessible by the code executor.

        Args:
            file_paths: List of storage keys for uploaded files
            retrieved_contexts: List of KB retrieval results

        Returns:
            Tuple of (input_files, kb_documents) for code executor
        """
        import mimetypes

        from .code_executor import InputFile, KBDocument

        input_files: list[InputFile] | None = None
        kb_documents: list[KBDocument] | None = None

        # Convert uploaded files to InputFile objects
        if file_paths and self.file_storage:
            input_files = []
            for path in file_paths:
                try:
                    # Extract filename from path
                    filename = path.split("/")[-1] if "/" in path else path

                    # Download file content from storage
                    content = await self.file_storage.download_file(path)

                    # Guess MIME type
                    mime_type, _ = mimetypes.guess_type(filename)

                    input_files.append(
                        InputFile(
                            filename=filename,
                            content=content,
                            mime_type=mime_type,
                        )
                    )
                    logger.debug(f"Prepared input file for code execution: {filename}")
                except Exception as e:
                    logger.warning(f"Failed to load file {path} for code execution: {e}")

            if not input_files:
                input_files = None

        # Convert KB contexts to KBDocument objects
        if retrieved_contexts:
            kb_documents = []
            for ctx in retrieved_contexts:
                for i, chunk in enumerate(ctx.chunks):
                    try:
                        chunk_text = (
                            chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
                        )
                        doc_id = chunk.get("document_id", "") if isinstance(chunk, dict) else None
                        metadata = chunk.get("metadata", {}) if isinstance(chunk, dict) else {}

                        kb_documents.append(
                            KBDocument(
                                filename=f"{ctx.dataset_id}_chunk_{i}.txt",
                                content=chunk_text,
                                document_id=doc_id,
                                metadata=metadata,
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Failed to convert KB chunk: {e}")

            if not kb_documents:
                kb_documents = None

        return input_files, kb_documents

