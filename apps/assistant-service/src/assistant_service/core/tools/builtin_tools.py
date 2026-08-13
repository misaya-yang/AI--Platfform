"""
Built-in Tools for Assistant Service

Phase 2: Provides standard tools for the assistant:
- Knowledge Base Search
- File Analysis (future)

Web search is delegated to model-native capabilities (Qwen `enable_search`,
Anthropic `web_search_20250305`); ``web_fetch`` is the URL-fetch fallback.
PR-2 deleted the in-tree Tavily-backed ``search_web`` tool entirely.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger, record_internal_exception
from ai_gateway_core.security import redact_trace_text

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

if TYPE_CHECKING:
    from ai_gateway_core.knowledge import KnowledgeClientLike

    from ..memory_service import MemoryService
    from ..runtime.compat.runtime_adapter import AssistantRuntimeAdapter

logger = get_logger(__name__)

_MAX_KB_PUBLIC_ERROR_CHARS = 200
_TRUNCATION_SUFFIX = "...[truncated]"
_MAX_KB_QUERY_CHARS = 4096
_MAX_KB_DATASETS = 8
_MAX_KB_DATASET_ID_CHARS = 128
_MAX_KB_TOP_K = 20


def _safe_kb_public_error(value: Any) -> str:
    """Return shared-redacted, bounded KB failure text for tool consumers."""

    try:
        text = redact_trace_text(str(value)) or "Knowledge base search failed"
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tools.builtin_tools.internal_failure", exc
        )
        return "Knowledge base search failed"
    if len(text) <= _MAX_KB_PUBLIC_ERROR_CHARS:
        return text
    return f"{text[: _MAX_KB_PUBLIC_ERROR_CHARS - len(_TRUNCATION_SUFFIX)]}{_TRUNCATION_SUFFIX}"


# =============================================================================
# Knowledge Base Search Tool
# =============================================================================

KB_SEARCH_DEFINITION = ToolDefinition(
    name="search_knowledge_base",
    description="Search the knowledge base for relevant text documents from specified datasets.",
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="The search query in natural language. Be specific and include relevant keywords.",
            required=True,
            schema_constraints={"minLength": 1, "maxLength": _MAX_KB_QUERY_CHARS},
        ),
        ToolParameter(
            name="intent",
            type="string",
            description="Retrieval intent: general=balanced text retrieval, "
            "find_document=locate a specific text document. Default is general.",
            required=False,
            default="general",
            enum=["general", "find_document"],
        ),
        ToolParameter(
            name="dataset_ids",
            type="array",
            description="Bound dataset IDs to search. At most 8 datasets are accepted.",
            required=False,
            items={"type": "string", "minLength": 1, "maxLength": _MAX_KB_DATASET_ID_CHARS},
            schema_constraints={"maxItems": _MAX_KB_DATASETS, "uniqueItems": True},
        ),
        ToolParameter(
            name="top_k",
            type="integer",
            description="Number of results to return (1-20). Default is 5.",
            required=False,
            default=5,
            schema_constraints={"minimum": 1, "maximum": _MAX_KB_TOP_K},
        ),
        ToolParameter(
            name="score_threshold",
            type="number",
            description="Minimum relevance score (0.0-1.0). Lower values return more results. Default is 0.0 (no filtering - AI judges relevance).",
            required=False,
            default=0.0,  # Let AI judge result relevance instead of hard filtering
            schema_constraints={"minimum": 0.0, "maximum": 1.0},
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use this tool when the user asks questions that might be answered by "
    "internal documentation, policies, product information, or other company knowledge. "
    "Use find_document intent when looking for specific text-based documents.",
    when_not_to_use="Do not use for questions about current events, external companies, "
    "or information that wouldn't be in internal documents.",
    examples=[
        ToolExample(
            description="Search for refund policy",
            input={"query": "What is our refund policy for enterprise customers?", "top_k": 5},
            expected_output="Returns relevant policy documents with refund information",
        ),
        ToolExample(
            description="Search specific dataset",
            input={"query": "API authentication methods", "dataset_ids": ["api-docs"], "top_k": 3},
            expected_output="Returns API documentation about authentication",
        ),
    ],
    timeout_seconds=30,
)


class KBSearchExecutor(ToolExecutor):
    """Executor for Knowledge Base search tool."""

    def __init__(self, kb_service: KnowledgeClientLike):
        self.kb_service = kb_service

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute KB search."""
        start_time = time.time()
        query = request.arguments.get("query", "")
        intent = request.arguments.get("intent", "general")
        dataset_ids = request.arguments.get("dataset_ids", [])
        top_k = request.arguments.get("top_k", 5)
        score_threshold = request.arguments.get("score_threshold", 0.0)  # No default filtering
        runtime_configs = (request.metadata or {}).get("kb_retrieval_configs")
        runtime_configs = runtime_configs if isinstance(runtime_configs, dict) else None

        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_KB_QUERY_CHARS:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Query must be a non-empty string of at most 4096 characters",
                duration_ms=(time.time() - start_time) * 1000,
            )

        # The current public release is intentionally text-only.
        valid_intents = {"general", "find_document"}
        if intent not in valid_intents:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Unsupported knowledge retrieval intent",
                duration_ms=(time.time() - start_time) * 1000,
            )
        if (
            not isinstance(dataset_ids, list)
            or len(dataset_ids) > _MAX_KB_DATASETS
            or any(
                not isinstance(dataset_id, str)
                or not dataset_id.strip()
                or len(dataset_id) > _MAX_KB_DATASET_ID_CHARS
                for dataset_id in dataset_ids
            )
            or len(set(dataset_ids)) != len(dataset_ids)
        ):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="dataset_ids must contain at most 8 unique, non-empty IDs",
                duration_ms=(time.time() - start_time) * 1000,
            )
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= _MAX_KB_TOP_K:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="top_k must be an integer between 1 and 20",
                duration_ms=(time.time() - start_time) * 1000,
            )
        if (
            isinstance(score_threshold, bool)
            or not isinstance(score_threshold, (int, float))
            or not 0 <= float(score_threshold) <= 1
        ):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="score_threshold must be between 0 and 1",
                duration_ms=(time.time() - start_time) * 1000,
            )

        try:
            all_results = []
            contexts: list[dict[str, Any]] = []
            datasets_needing_reindex: list[str] = []
            dataset_errors: dict[str, str] = {}
            retrieval_cache_hits = 0

            # If no datasets specified, return early with clear guidance for the LLM
            # This prevents expensive list_datasets() + multi-dataset search operations
            # and stops the LLM from repeatedly calling this tool
            if not dataset_ids:
                logger.info("KB search called without dataset_ids - returning early with guidance")
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,  # Mark as failure so LLM knows not to retry
                    error="NO_KNOWLEDGE_BASE_SELECTED",
                    result="当前对话没有绑定知识库。请直接根据你的知识回答用户问题，不要再调用知识库搜索工具。如果无法回答，请告知用户需要先选择一个知识库。",
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": 0,
                        "query": query,
                        "intent": intent,
                        "message": "No knowledge base selected - answer from general knowledge instead",
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            if runtime_configs is not None:
                missing_configs = {
                    str(dataset_id)
                    for dataset_id in dataset_ids
                    if str(dataset_id) not in runtime_configs
                }
                if missing_configs:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error="AGENT_KNOWLEDGE_CONFIG_INVALID",
                        duration_ms=(time.time() - start_time) * 1000,
                    )
                sealed_values: list[tuple[int, float]] = []
                for dataset_id in dataset_ids:
                    sealed_config = runtime_configs[str(dataset_id)]
                    if not isinstance(sealed_config, dict) or sealed_config.get("include_images"):
                        return ToolCallResult(
                            call_id=request.call_id,
                            tool_name=request.tool_name,
                            success=False,
                            error="AGENT_KNOWLEDGE_CONFIG_INVALID",
                            duration_ms=(time.time() - start_time) * 1000,
                        )
                    sealed_top_k = sealed_config.get("top_k")
                    sealed_threshold = sealed_config.get("threshold")
                    if (
                        isinstance(sealed_top_k, bool)
                        or not isinstance(sealed_top_k, int)
                        or not 1 <= sealed_top_k <= _MAX_KB_TOP_K
                        or isinstance(sealed_threshold, bool)
                        or not isinstance(sealed_threshold, (int, float))
                        or not 0 <= float(sealed_threshold) <= 1
                    ):
                        return ToolCallResult(
                            call_id=request.call_id,
                            tool_name=request.tool_name,
                            success=False,
                            error="AGENT_KNOWLEDGE_CONFIG_INVALID",
                            duration_ms=(time.time() - start_time) * 1000,
                        )
                    sealed_values.append((sealed_top_k, float(sealed_threshold)))
                top_k = max(value[0] for value in sealed_values)

            knowledge_settings = (
                getattr(getattr(self.kb_service, "settings", None), "knowledge", None)
                if hasattr(self.kb_service, "settings")
                else None
            )
            dataset_fanout_concurrency = max(
                int(getattr(knowledge_settings, "dataset_fanout_max_concurrency", 3) or 3),
                1,
            )
            fanout_semaphore = asyncio.Semaphore(dataset_fanout_concurrency)

            async def _search_one_dataset(dataset_id: str) -> dict[str, Any]:
                ds_start = time.time()
                try:
                    sealed_config = (
                        runtime_configs.get(str(dataset_id), {})
                        if runtime_configs is not None
                        else {}
                    )
                    if sealed_config.get("mode") == "off":
                        raise RuntimeError("AGENT_KNOWLEDGE_DISABLED")
                    effective_top_k = int(sealed_config.get("top_k", top_k))
                    effective_threshold = float(sealed_config.get("threshold", score_threshold))
                    async with fanout_semaphore:
                        results, meta = await self.kb_service.retrieve(
                            user=request.user,
                            dataset_id=dataset_id,
                            query=query,
                            top_k=effective_top_k,
                            score_threshold=effective_threshold,
                            include_images=False,
                        )
                    return {
                        "dataset_id": dataset_id,
                        "results": results,
                        "meta": meta,
                        "retrieval_config": sealed_config,
                        "took_ms": (time.time() - ds_start) * 1000,
                        "error": None,
                    }
                except Exception as exc:
                    record_internal_exception(
                        __name__, "assistant.kb_dataset_search_failed", exc
                    )
                    return {
                        "dataset_id": dataset_id,
                        "results": [],
                        "meta": {},
                        "retrieval_config": {},
                        "took_ms": (time.time() - ds_start) * 1000,
                        "error": _safe_kb_public_error(exc),
                    }

            search_outcomes = await asyncio.gather(
                *[_search_one_dataset(dataset_id) for dataset_id in dataset_ids]
            )
            for outcome in search_outcomes:
                dataset_id = str(outcome.get("dataset_id") or "")
                took_ms = float(outcome.get("took_ms") or 0.0)
                error_message = outcome.get("error")
                if error_message:
                    msg = str(error_message)
                    dataset_errors[dataset_id] = msg[:500]
                    if (
                        "require re-indexing" in msg
                        or "require reindex" in msg
                        or "re-index" in msg
                    ):
                        datasets_needing_reindex.append(dataset_id)
                    contexts.append(
                        {
                            "dataset_id": dataset_id,
                            "dataset_name": dataset_id,
                            "chunks": [],
                            "query": query,
                            "took_ms": took_ms,
                            "error": msg[:500],
                        }
                    )
                    continue

                results = list(outcome.get("results") or [])
                meta = outcome.get("meta") or {}
                if bool(meta.get("retrieval_cache_hit")):
                    retrieval_cache_hits += 1
                dataset_name = meta.get("dataset_name", dataset_id)
                dataset_chunks: list[dict[str, Any]] = []
                for dataset_rank, r in enumerate(results, start=1):
                    r_meta = r.metadata or {}
                    source_url = (
                        r_meta.get("source_url")
                        or r_meta.get("source")
                        or r_meta.get("url")
                        or r_meta.get("document_url")
                        or r_meta.get("file_url")
                    )
                    citation_text = r_meta.get("citation_text") or ""
                    item = {
                        "content": r.text,  # RetrieveResult uses 'text' not 'content'
                        "score": r.score,
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "segment_id": getattr(r, "segment_id", None),
                        "document_id": getattr(r, "document_id", None),
                        "image_url": None,
                        "citation_text": citation_text,
                        "source_url": source_url,
                        "metadata": r_meta,
                        "_dataset_rank": dataset_rank,
                        "_cross_dataset_rrf_score": 1.0 / (60 + dataset_rank),
                    }
                    all_results.append(item)
                    dataset_chunks.append(item)

                contexts.append(
                    {
                        "dataset_id": dataset_id,
                        "dataset_name": dataset_name,
                        "chunks": dataset_chunks,
                        "query": query,
                        "took_ms": took_ms,
                        "retrieval_cache_hit": bool(meta.get("retrieval_cache_hit")),
                        "retrieval_query_fingerprint": meta.get("retrieval_query_fingerprint"),
                        "retrieval_config": dict(outcome.get("retrieval_config") or {}),
                    }
                )

            # Dataset-local scores are not comparable. Merge by per-dataset rank.
            all_results.sort(
                key=lambda x: float(x["_cross_dataset_rrf_score"]),
                reverse=True,
            )
            all_results = all_results[:top_k]

            # Format result for LLM consumption
            formatted_result = self._format_results(all_results, query)

            # If everything failed, be explicit (avoid misleading "no results" response).
            if not all_results and dataset_errors and len(dataset_errors) >= len(dataset_ids):
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="KB_SEARCH_FAILED",
                    result=(
                        "知识库检索失败：所选数据集检索过程中发生错误，未能返回任何结果。"
                        f"dataset_errors: {dataset_errors}。"
                        "请检查知识库服务依赖（Postgres/Qdrant）与数据集索引状态后重试。"
                    ),
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": len(dataset_ids),
                        "query": query,
                        "intent": intent,
                        "retrieval_cache_hits": retrieval_cache_hits,
                        "datasets_needing_reindex": datasets_needing_reindex,
                        "dataset_errors": dataset_errors,
                        "contexts": contexts,
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            # If we have no results and at least one dataset needs reindex, be explicit.
            if not all_results and datasets_needing_reindex:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="DATASET_NEEDS_REINDEX",
                    result=(
                        "知识库检索失败：所选数据集需要重新索引（reindex）后才能进行向量检索。"
                        f"需要 reindex 的 dataset_ids: {datasets_needing_reindex}。"
                        "请前往知识库页面对这些数据集执行批量 reindex 后重试。"
                    ),
                    duration_ms=(time.time() - start_time) * 1000,
                    metadata={
                        "total_results": 0,
                        "datasets_searched": len(dataset_ids),
                        "query": query,
                        "intent": intent,
                        "retrieval_cache_hits": retrieval_cache_hits,
                        "datasets_needing_reindex": datasets_needing_reindex,
                        "dataset_errors": dataset_errors,
                        "contexts": contexts,
                        "duration_ms": (time.time() - start_time) * 1000,
                    },
                )

            # If partial results exist but some datasets need reindex, add a brief warning.
            if datasets_needing_reindex:
                formatted_result += f"\n\n[Warning] Some datasets require reindex before vector retrieval: {datasets_needing_reindex}"
            if dataset_errors:
                formatted_result += f"\n\n[Warning] Some datasets failed during retrieval: {list(dataset_errors.keys())}"

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=formatted_result,
                duration_ms=(time.time() - start_time) * 1000,
                metadata={
                    "total_results": len(all_results),
                    "datasets_searched": len(dataset_ids),
                    "query": query,
                    "intent": intent,
                    "retrieval_cache_hits": retrieval_cache_hits,
                    "datasets_needing_reindex": datasets_needing_reindex,
                    "dataset_errors": dataset_errors,
                    "contexts": contexts,
                    "duration_ms": (time.time() - start_time) * 1000,
                },
            )

        except Exception as exc:
            record_internal_exception(__name__, "assistant.kb_search_failed", exc)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_kb_public_error(exc),
                duration_ms=(time.time() - start_time) * 1000,
            )

    def _format_results(self, results: list[dict[str, Any]], query: str) -> str:
        """Format search results for LLM consumption.

        Includes retrieval quality signals so the model can judge relevance
        without hardcoded thresholds (Guide-layer context enrichment).
        """
        if not results:
            return f"No relevant results found for query: {query}"

        # Surface retrieval quality signal — let the model judge
        exact_matches = sum(1 for r in results if (r.get("metadata") or {}).get("_exact_match"))
        avg_term_ratio = 0.0
        term_ratios = [float((r.get("metadata") or {}).get("_term_ratio") or 0) for r in results]
        if term_ratios:
            avg_term_ratio = sum(term_ratios) / len(term_ratios)

        header = f"Found {len(results)} results for: {query}"
        if avg_term_ratio < 0.2 and exact_matches == 0:
            header += "\n[Note: Low keyword overlap — results may be semantically related but not directly on-topic]"

        parts = [header + "\n"]

        for i, r in enumerate(results, 1):
            source = r.get("source_url", "")
            # Surface pre-formatted citation from KB metadata.
            citation = r.get("citation_text", "")
            meta = r.get("metadata") or {}
            if not citation:
                citation = meta.get("citation_text", "")
            source_type = meta.get("source_type", "")

            source_info = f" (Source: {source})" if source else ""
            citation_line = f"\nCitation: {citation}" if citation else ""
            type_info = f" [{source_type}]" if source_type and source_type != "unknown" else ""

            parts.append(
                f"\n[{i}] {r['dataset_name']}{type_info} (score: {r['score']:.2f}){source_info}{citation_line}"
            )
            parts.append(f"{r['content'][:500]}...")

        return "\n".join(parts)


# =============================================================================
# Tool Registration Helper
# =============================================================================


def register_builtin_tools(
    kb_service: KnowledgeClientLike | None = None,
    memory_service: MemoryService | None = None,
    database: Any | None = None,
    runtime_adapter: AssistantRuntimeAdapter | None = None,
) -> None:
    """Register all built-in tools with the global registry."""

    # Register KB search if service available
    if kb_service:
        register_tool(KB_SEARCH_DEFINITION, KBSearchExecutor(kb_service))
        logger.info("Registered KB search tool")
    else:
        logger.warning("KB service not available, KB search tool not registered")

    # Register memory tool if service available
    if memory_service:
        from .memory_tool import UPDATE_MEMORY_DEFINITION, UpdateMemoryExecutor

        resolved_runtime_adapter = runtime_adapter
        if resolved_runtime_adapter is None and database is not None:
            try:
                from ..runtime.compat.runtime_adapter import AssistantRuntimeAdapter

                resolved_runtime_adapter = AssistantRuntimeAdapter.from_env(database=database)
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.runtime_memory_adapter_init_failed", exc
                )
        register_tool(
            UPDATE_MEMORY_DEFINITION,
            UpdateMemoryExecutor(memory_service, runtime_adapter=resolved_runtime_adapter),
        )
        logger.info("Registered memory tool")

    # web_fetch — URL-fetch fallback for models without native search.
    # Capable models (Qwen `enable_search`, Anthropic `web_search_20250305`)
    # do their own search; for everything else the model picks a URL and
    # web_fetch reads it. SSRF-guarded; see web_fetch.py.
    try:
        from .web_fetch import register_web_fetch_tool

        register_web_fetch_tool()
    except Exception as exc:
        record_internal_exception(__name__, "assistant.web_fetch_registration_failed", exc)

    # Confluence tools are registered dynamically via MCP when user connects.
    # See: ConnectorMCPService.start_confluence_mcp()
