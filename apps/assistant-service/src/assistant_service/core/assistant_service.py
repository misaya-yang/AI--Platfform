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
import os
import time
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
from .rag.scenario_analyzer import (
    ScenarioDetectionResult,
    create_scenario_analyzer,
)
from .content.structured_output import (
    OutputFormat,
    OutputGuardrail,
)
from .tasks.task_planner import TaskPlanner
from .tool_orchestrator import ToolOrchestrator
from .tools.code_executor_tool import CODE_EXECUTOR_TOOL, CodeExecutorToolExecutor
from .working_memory import WorkingMemory

logger = get_logger(__name__)


from ai_gateway_core.enums import RAGMode  # noqa: E402 — re-export for AS-internal sites

# ``RAGMode`` is now defined in ``ai_gateway_core.enums`` so gateway routes
# (assistant.py) can import the enum without pulling in ``assistant_service``.
# Kept as a local re-export until AS-internal imports migrate to the shared
# module directly.


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

        # ========== Agent Loop (only execution path) ==========
        # The legacy 8-step ReAct pipeline that lived below this point has
        # been removed; ``_execute_agent_loop`` is now the sole path.
        # ``config.use_agent_loop`` is retained on AssistantConfig for
        # frontend-schema parity but no longer gates a separate code path.
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
                    from ai_gateway_core.knowledge import get_authority_order

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

