"""
Assistant service module.

Provides a GPT-like assistant experience with:
- Multi-model support (OpenAI, Claude, DeepSeek, Qwen)
- Knowledge base integration
- Streaming responses
- RAG evaluation and citation tracking (Phase 3)
- Structured output and validation (Phase 4)
- Write-while-search capability (Phase 2.3)
"""

from .agent.agent_loop import (
    PHASE_DISPLAY_NAMES,
    PHASE_INDEX,
    TOTAL_PHASES,
    AgentLoop,
    AgentLoopConfig,
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    ErrorSeverity,
    StructuredError,
    create_agent_loop,
)
from .agent.agui_protocol import (
    AGUIEventEmitter,
    ArtifactEvent,
    BaseEvent,
    RunLifecycleEvent,
    StateEvent,
    StatusEvent,
    StepEvent,
    TextMessageEvent,
    ToolCallEvent,
    create_agui_emitter,
)
from .assistant_service import AssistantConfig, AssistantService, RAGEvaluation
from .quality.cache_optimizer import (
    CacheBreakpoint,
    CacheConfig,
    CacheMetrics,
    ContextCacheOptimizer,
)
from .content.content_generator import (
    ContentOutline,
    ContentSection,
    DeepContentGenerator,
    GeneratedContent,
    GenerationPhase,
    StreamEvent,
    create_content_generator,
)
from .rag.context_metrics import (
    CacheMetrics as KVCacheMetrics,  # Renamed to avoid conflict with cache_optimizer.CacheMetrics
)
from .rag.context_metrics import (
    CompressionMetrics,
    ContextMetrics,
    ContextMetricsBuilder,
    ContextMetricsCollector,
    LayerMetrics,
    MemoryMetrics,
    MetricLayer,
    get_context_metrics_collector,
    init_context_metrics_collector,
)
from .files.document_parser import DocumentParseError, DocumentParser, parse_document
from .files.file_processor import (
    FileProcessError,
    FileProcessor,
    ImageContent,
    ProcessedFiles,
    create_file_processor,
)
from .quality.guardrails import (
    BANNED_PHRASES,
    QUALITY_THRESHOLDS,
    TOOL_CONSTRAINTS,
    DocumentType,
    IssueSeverity,
    QualityGuardrails,
    QualityIssue,
    ToolCallValidation,
    ToolConstraintValidator,
    ValidationResult,
)
from .models.model_registry import ModelInfo, ModelProvider, ModelRegistry

# Query Intent Analyzer (Self-RAG style adaptive retrieval)
from .rag.query_intent_analyzer import (
    QueryIntent,
    QueryIntentAnalyzer,
    QueryType,
    RetrievalDecision,
    create_query_intent_analyzer,
)
from .rag.rag_metrics import (
    Citation,
    CitationStatus,
    ContextChunkMetrics,
    RAGEvaluator,
    RAGMetrics,
    RAGMetricsCollector,
    RetrievalMetrics,
    evaluate_rag,
    extract_citations,
    get_rag_evaluator,
    get_rag_metrics_collector,
)
from .content.streaming_writer import (
    DEFAULT_VERIFICATION_TRIGGERS,
    StreamChunk,
    StreamingWriter,
    create_streaming_writer,
)
from .content.structured_output import (
    AnswerWithCitations,
    ClassificationResult,
    ExtractedEntities,
    FactCheckResult,
    OutputFormat,
    OutputGuardrail,
    StepByStepAnswer,
    StructuredOutputParser,
    StructuredOutputResult,
    create_json_prompt,
    parse_structured_output,
    validate_output,
)
from .tasks.task_manager import (
    SessionResources,
    TaskContext,
    TaskManager,
    get_task_manager,
    init_task_manager,
    shutdown_task_manager,
)
from .tasks.task_planner import IntentType, TaskStrategy

# Enterprise Agent Loop Components
from .tool_invoker import (
    RegistryToolInvoker,
    ToolInvocationContext,
    ToolInvoker,
    create_tool_invoker,
)
from .gateway import (
    AssistantExecutionGateway,
    AssistantPolicyEngine,
    AssistantRequestRouter,
    RoutedAssistantRequest,
    ToolPolicyDecision,
)

__all__ = [
    "AssistantService",
    "AssistantConfig",
    "RAGEvaluation",
    "ModelRegistry",
    "ModelProvider",
    "ModelInfo",
    # Phase 3: RAG Metrics
    "RAGEvaluator",
    "RAGMetrics",
    "Citation",
    "CitationStatus",
    "ContextChunkMetrics",
    "get_rag_evaluator",
    "evaluate_rag",
    "extract_citations",
    # Phase 4: Structured Output
    "OutputFormat",
    "OutputGuardrail",
    "StructuredOutputParser",
    "StructuredOutputResult",
    "AnswerWithCitations",
    "StepByStepAnswer",
    "FactCheckResult",
    "ExtractedEntities",
    "ClassificationResult",
    "parse_structured_output",
    "validate_output",
    "create_json_prompt",
    # Cache Optimization
    "CacheConfig",
    "CacheMetrics",
    "CacheBreakpoint",
    "ContextCacheOptimizer",
    # Document Parser
    "DocumentParser",
    "DocumentParseError",
    "parse_document",
    # File Processor
    "FileProcessor",
    "ProcessedFiles",
    "ImageContent",
    "FileProcessError",
    "create_file_processor",
    # StreamingWriter (Phase 2.3: Write-while-search)
    "StreamChunk",
    "StreamingWriter",
    "create_streaming_writer",
    "DEFAULT_VERIFICATION_TRIGGERS",
    # Guardrails (Quality Control)
    "DocumentType",
    "IssueSeverity",
    "QualityIssue",
    "ValidationResult",
    "QualityGuardrails",
    "ToolCallValidation",
    "ToolConstraintValidator",
    "QUALITY_THRESHOLDS",
    "BANNED_PHRASES",
    "TOOL_CONSTRAINTS",
    # Deep Content Generator (Phase 2)
    "GenerationPhase",
    "ContentSection",
    "ContentOutline",
    "GeneratedContent",
    "StreamEvent",
    "DeepContentGenerator",
    "create_content_generator",
    # Intent Analysis (Phase 2)
    "IntentType",
    "TaskStrategy",
    # AG-UI Protocol (Phase 3)
    "AGUIEventEmitter",
    "BaseEvent",
    "RunLifecycleEvent",
    "StepEvent",
    "TextMessageEvent",
    "ToolCallEvent",
    "StateEvent",
    "ArtifactEvent",
    "StatusEvent",
    "create_agui_emitter",
    # Enterprise Agent Loop Components
    "ToolInvoker",
    "ToolInvocationContext",
    "RegistryToolInvoker",
    "create_tool_invoker",
    "AssistantExecutionGateway",
    "AssistantPolicyEngine",
    "ToolPolicyDecision",
    "AssistantRequestRouter",
    "RoutedAssistantRequest",
    "TaskManager",
    "SessionResources",
    "TaskContext",
    "get_task_manager",
    "init_task_manager",
    "shutdown_task_manager",
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopContext",
    "AgentLoopEvent",
    "AgentLoopPhase",
    "ErrorSeverity",
    "StructuredError",
    "PHASE_DISPLAY_NAMES",
    "PHASE_INDEX",
    "TOTAL_PHASES",
    "create_agent_loop",
    "RetrievalMetrics",
    "RAGMetricsCollector",
    "get_rag_metrics_collector",
    # Context Metrics (Observability)
    "ContextMetrics",
    "ContextMetricsBuilder",
    "ContextMetricsCollector",
    "MetricLayer",
    "LayerMetrics",
    "CompressionMetrics",
    "KVCacheMetrics",  # Renamed from CacheMetrics to avoid conflict
    "MemoryMetrics",
    "get_context_metrics_collector",
    "init_context_metrics_collector",
    # Query Intent Analyzer (Self-RAG style)
    "QueryType",
    "RetrievalDecision",
    "QueryIntent",
    "QueryIntentAnalyzer",
    "create_query_intent_analyzer",
]
