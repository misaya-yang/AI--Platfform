"""
KB Tools API - Universal Knowledge Base Access for LangGraph Agents

This module provides a clean, minimal API surface for knowledge base operations,
optimized for LangGraph agent consumption.

Primary Endpoints:
- POST /kb/search - Search a single dataset
- POST /kb/multi-search - Search across multiple datasets
- GET /kb/datasets - List available datasets
- GET /kb/tool-definition/{dataset_id} - Get OpenAI function calling definition

Design Principles:
- Simple request/response format
- Sensible defaults for all parameters
- Pre-formatted context strings for direct LLM injection
- Compatible with LangChain tool patterns
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_knowledge_service, get_user_context
from ..schemas.kb_tools import (
    KBAssociatedImage,
    KBDatasetInfo,
    KBListDatasetsResponse,
    KBMultiSearchRequest,
    KBMultiSearchResponse,
    KBSearchRequest,
    KBSearchResponse,
    KBSearchResult,
    KBToolDefinition,
    get_kb_search_tool_definition,
    get_multi_kb_search_tool_definition,
)
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...services.knowledge.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/kb", tags=["KB Tools"])


# ============================================================
# Helper Functions
# ============================================================

def _format_context_for_llm(
    results: List[KBSearchResult],
    query: str,
    max_length: int = 8000,
) -> str:
    """
    Format search results as a context string for LLM consumption.

    Output format:
    [1] (score: 0.85)
    Content here...

    [2] (score: 0.72)
    More content...
    """
    if not results:
        return "No relevant information found in the knowledge base."

    formatted_parts = []
    current_length = 0

    for i, r in enumerate(results, 1):
        # Build the entry
        entry_header = f"[{i}] (score: {r.score:.3f})"

        # Include VLM description for image segments
        if r.content_type == "image" and r.vlm_description:
            content = f"[Image: {r.vlm_description}]"
        else:
            content = r.content

        entry = f"{entry_header}\n{content}"

        # Check length limit
        if current_length + len(entry) + 10 > max_length:
            if formatted_parts:
                formatted_parts.append("... (additional results truncated)")
            break

        formatted_parts.append(entry)
        current_length += len(entry) + 4  # Account for separator

    return "\n\n---\n\n".join(formatted_parts)


def _convert_retrieve_result_to_search_result(
    result: Any,
    dataset_id: str,
) -> KBSearchResult:
    """Convert internal RetrieveResult to KBSearchResult."""
    # Handle associated images
    associated_images = []
    if hasattr(result, "associated_images") and result.associated_images:
        for img in result.associated_images:
            associated_images.append(KBAssociatedImage(
                image_segment_id=getattr(img, "image_segment_id", ""),
                storage_url=getattr(img, "storage_url", ""),
                filename=getattr(img, "filename", ""),
                vlm_description=getattr(img, "vlm_description", None),
                proximity_score=getattr(img, "proximity_score", 1.0),
                media_type=getattr(img, "media_type", "image/png"),
            ))

    return KBSearchResult(
        content=result.text,
        score=result.score,
        segment_id=result.segment_id,
        document_id=result.document_id,
        dataset_id=dataset_id,
        content_type=getattr(result, "content_type", "text"),
        metadata=result.metadata or {},
        image_url=getattr(result, "image_url", None),
        vlm_description=getattr(result, "vlm_description", None),
        associated_images=associated_images,
    )


def _resolve_mode(mode: str, query: str) -> str:
    """
    Resolve 'auto' mode to actual retrieval mode based on query characteristics.

    Heuristics:
    - Short queries (< 3 words): prefer keyword (bm25)
    - Questions with specific terms: prefer hybrid
    - Long queries: prefer dense (semantic)
    """
    if mode != "auto":
        return mode

    words = query.strip().split()
    word_count = len(words)

    # Very short queries benefit from keyword matching
    if word_count <= 2:
        return "hybrid"  # Still use hybrid for balance

    # Most queries benefit from hybrid
    return "hybrid"


# ============================================================
# API Endpoints
# ============================================================

@router.post("/search", response_model=KBSearchResponse)
async def kb_search(
    request: KBSearchRequest,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> KBSearchResponse:
    """
    Search a knowledge base for relevant information.

    This is the primary endpoint for LangGraph agents to access KB content.
    Returns both structured results and a pre-formatted context string.

    Example Request:
    ```json
    {
        "query": "What is our refund policy?",
        "dataset_id": "company-docs",
        "top_k": 5
    }
    ```

    Example Response:
    ```json
    {
        "results": [...],
        "formatted_context": "[1] (score: 0.85)\\nOur refund policy allows...",
        "query": "What is our refund policy?",
        "dataset_id": "company-docs",
        "total_results": 5
    }
    ```
    """
    t_start = time.perf_counter()

    try:
        # Verify dataset access
        await svc.require_dataset_access(user, request.dataset_id, required="viewer")

        # Resolve auto mode
        mode = _resolve_mode(request.mode, request.query)

        # Determine if multimodal retrieval is needed
        needs_multimodal = (
            request.include_images
            or request.include_associated_images
            or request.content_type_filter is not None
        )

        if needs_multimodal:
            # Use multimodal-aware retrieval with image support
            results, meta = await svc.retrieve_with_images(
                user=user,
                dataset_id=request.dataset_id,
                query=request.query,
                top_k=request.top_k,
                include_images=request.include_images,
                content_type_filter=request.content_type_filter,
                mode=mode,
                document_id=request.document_id,
                rerank=request.rerank,
                mmr=request.mmr,
                score_threshold=request.score_threshold,
            )
        else:
            # Standard retrieval (faster when multimodal not needed)
            results, meta = await svc.retrieve(
                user=user,
                dataset_id=request.dataset_id,
                query=request.query,
                top_k=request.top_k,
                mode=mode,
                document_id=request.document_id,
                rerank=request.rerank,
                mmr=request.mmr,
                score_threshold=request.score_threshold,
            )

        # Convert results
        search_results = [
            _convert_retrieve_result_to_search_result(r, request.dataset_id)
            for r in results
        ]

        # Format context for LLM
        formatted_context = _format_context_for_llm(search_results, request.query)

        # Build response
        t_elapsed = (time.perf_counter() - t_start) * 1000

        return KBSearchResponse(
            results=search_results,
            formatted_context=formatted_context,
            query=request.query,
            dataset_id=request.dataset_id,
            total_results=len(search_results),
            metadata={
                "mode": mode,
                "rerank": request.rerank,
                "mmr": request.mmr,
                "latency_ms": round(t_elapsed, 1),
                **(meta or {}),
            },
        )

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception(f"KB search failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Search failed: {str(exc)}")


@router.post("/multi-search", response_model=KBMultiSearchResponse)
async def kb_multi_search(
    request: KBMultiSearchRequest,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> KBMultiSearchResponse:
    """
    Search across multiple knowledge bases.

    Results are merged and ranked according to the specified strategy:
    - score: Sort by relevance score (default)
    - round_robin: Interleave results from each dataset
    - rrf: Reciprocal Rank Fusion

    Example Request:
    ```json
    {
        "query": "How do I reset my password?",
        "dataset_ids": ["docs", "wiki", "faq"],
        "top_k": 5
    }
    ```
    """
    import asyncio

    t_start = time.perf_counter()

    # Verify access to all datasets first
    accessible_datasets = []
    for ds_id in request.dataset_ids:
        try:
            await svc.require_dataset_access(user, ds_id, required="viewer")
            accessible_datasets.append(ds_id)
        except (PermissionDeniedError, ValidationFailedError):
            logger.warning(f"User {user.user_id} cannot access dataset {ds_id}")

    if not accessible_datasets:
        raise HTTPException(
            status_code=403,
            detail="No accessible datasets found"
        )

    # Resolve mode
    mode = _resolve_mode(request.mode, request.query)

    # Search each dataset in parallel
    async def search_dataset(ds_id: str) -> tuple[str, List[Any], Dict[str, Any]]:
        try:
            results, meta = await svc.retrieve(
                user=user,
                dataset_id=ds_id,
                query=request.query,
                top_k=request.top_k,  # Get top_k from each, then merge
                mode=mode,
                rerank=request.rerank,
                mmr=request.mmr,
                score_threshold=request.score_threshold,
            )
            return ds_id, results, meta
        except Exception as exc:
            logger.warning(f"Search failed for dataset {ds_id}: {exc}")
            return ds_id, [], {}

    search_tasks = [search_dataset(ds_id) for ds_id in accessible_datasets]
    search_results_raw = await asyncio.gather(*search_tasks)

    # Merge results
    all_results: List[KBSearchResult] = []
    results_per_dataset: Dict[str, int] = {}

    for ds_id, results, meta in search_results_raw:
        results_per_dataset[ds_id] = len(results)
        for r in results:
            all_results.append(
                _convert_retrieve_result_to_search_result(r, ds_id)
            )

    # Apply merge strategy
    if request.merge_strategy == "score":
        all_results.sort(key=lambda x: x.score, reverse=True)
    elif request.merge_strategy == "round_robin":
        # Interleave results by dataset
        by_dataset = {ds_id: [] for ds_id in accessible_datasets}
        for r in all_results:
            by_dataset[r.dataset_id].append(r)

        interleaved = []
        max_len = max(len(v) for v in by_dataset.values()) if by_dataset else 0
        for i in range(max_len):
            for ds_id in accessible_datasets:
                if i < len(by_dataset[ds_id]):
                    interleaved.append(by_dataset[ds_id][i])
        all_results = interleaved
    elif request.merge_strategy == "rrf":
        # Reciprocal Rank Fusion
        k = 60
        rrf_scores: Dict[str, float] = {}
        for ds_id, results, meta in search_results_raw:
            for rank, r in enumerate(results, 1):
                key = f"{r.segment_id}"
                rrf_scores[key] = rrf_scores.get(key, 0) + 1.0 / (k + rank)

        # Update scores and sort
        for r in all_results:
            key = f"{r.segment_id}"
            r.score = rrf_scores.get(key, 0)
        all_results.sort(key=lambda x: x.score, reverse=True)

    # Take top_k
    all_results = all_results[:request.top_k]

    # Format context
    formatted_context = _format_context_for_llm(all_results, request.query)

    t_elapsed = (time.perf_counter() - t_start) * 1000

    return KBMultiSearchResponse(
        results=all_results,
        formatted_context=formatted_context,
        query=request.query,
        dataset_ids=accessible_datasets,
        total_results=len(all_results),
        results_per_dataset=results_per_dataset,
        metadata={
            "mode": mode,
            "merge_strategy": request.merge_strategy,
            "latency_ms": round(t_elapsed, 1),
        },
    )


@router.get("/datasets", response_model=KBListDatasetsResponse)
async def list_datasets(
    include_stats: bool = Query(default=False, description="Include document/segment counts"),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> KBListDatasetsResponse:
    """
    List knowledge bases available to the current user.

    Use this to discover what datasets an agent can search.
    """
    try:
        datasets_raw = await svc.list_datasets(user)

        datasets = []
        for ds in datasets_raw:
            info = KBDatasetInfo(
                dataset_id=ds.get("dataset_id", ""),
                name=ds.get("name", ""),
                description=ds.get("description", ""),
                embedding_provider=ds.get("embedding_provider", ""),
                embedding_model=ds.get("embedding_model", ""),
            )

            if include_stats:
                # Get stats if requested
                try:
                    stats = await svc.get_dataset_statistics(user, ds.get("dataset_id", ""))
                    info.document_count = stats.get("document_count", 0)
                    info.segment_count = stats.get("segment_count", 0)
                except Exception:
                    pass

            datasets.append(info)

        return KBListDatasetsResponse(
            datasets=datasets,
            total=len(datasets),
        )

    except Exception as exc:
        logger.exception(f"Failed to list datasets: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/tool-definition/{dataset_id}", response_model=KBToolDefinition)
async def get_tool_definition(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> KBToolDefinition:
    """
    Get OpenAI function calling definition for a dataset.

    Use this to dynamically generate tool definitions for LangChain agents.

    Example Response:
    ```json
    {
        "type": "function",
        "function": {
            "name": "search_company_docs",
            "description": "Search the 'Company Docs' knowledge base...",
            "parameters": {...}
        }
    }
    ```
    """
    try:
        dataset = await svc.require_dataset_access(user, dataset_id, required="viewer")
        dataset_name = dataset.get("name", dataset_id)

        return get_kb_search_tool_definition(dataset_id, dataset_name)

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/tool-definitions", response_model=List[KBToolDefinition])
async def get_all_tool_definitions(
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> List[KBToolDefinition]:
    """
    Get tool definitions for all accessible datasets.

    Returns a list of OpenAI function calling definitions.
    """
    try:
        datasets_raw = await svc.list_datasets(user)

        definitions = []
        for ds in datasets_raw:
            ds_id = ds.get("dataset_id", "")
            ds_name = ds.get("name", ds_id)
            definitions.append(get_kb_search_tool_definition(ds_id, ds_name))

        return definitions

    except Exception as exc:
        logger.exception(f"Failed to get tool definitions: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/multi-tool-definition")
async def get_multi_search_tool_definition(
    dataset_ids: str = Query(..., description="Comma-separated dataset IDs"),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
) -> KBToolDefinition:
    """
    Get a single tool definition for searching multiple datasets.

    Query param: dataset_ids (comma-separated)

    Example: /kb/multi-tool-definition?dataset_ids=docs,wiki,faq
    """
    ds_ids = [d.strip() for d in dataset_ids.split(",") if d.strip()]

    if not ds_ids:
        raise HTTPException(status_code=400, detail="No dataset IDs provided")

    # Verify access and get names
    dataset_names = {}
    accessible_ids = []

    for ds_id in ds_ids:
        try:
            ds = await svc.require_dataset_access(user, ds_id, required="viewer")
            accessible_ids.append(ds_id)
            dataset_names[ds_id] = ds.get("name", ds_id)
        except (PermissionDeniedError, ValidationFailedError):
            pass

    if not accessible_ids:
        raise HTTPException(status_code=403, detail="No accessible datasets")

    return get_multi_kb_search_tool_definition(accessible_ids, dataset_names)
