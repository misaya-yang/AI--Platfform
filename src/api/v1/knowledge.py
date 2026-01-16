from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from urllib.parse import unquote, urlparse

from ..deps import get_knowledge_service, get_knowledge_worker, get_user_context, get_settings
from ...core.crypto import verify_signed_url, get_unsigned_url

logger = logging.getLogger(__name__)
from ..schemas.knowledge import (
    BatchDeleteSchema,
    BatchReindexSchema,
    BatchRetrieveRequestSchema,
    ChunkingConfigSchema,
    DatasetConfigUpdateSchema,
    ChunkPreviewRequestSchema,
    ChunkPreviewResponseSchema,
    DatasetCreateSchema,
    DatasetPermissionGrantSchema,
    DatasetUpdateSchema,
    DocumentArchiveSchema,
    DocumentBatchCreateSchema,
    DocumentCreateTextSchema,
    DocumentCreateUrlSchema,
    DocumentEnableDisableSchema,
    DocumentUpdateSchema,
    LLMConfigSchema,
    QABatchTestSchema,
    QAQuerySchema,
    RetrievalConfigSchema,
    RetrieveRequestSchema,
    SegmentCreateSchema,
    SegmentEnableDisableSchema,
    SegmentUpdateSchema,
)
from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...services.knowledge.knowledge_service import KnowledgeService
from ...services.knowledge.worker import KnowledgeWorker


router = APIRouter()


@router.get("/knowledge/datasets")
async def list_datasets(
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    return await svc.list_datasets(user)


@router.post("/knowledge/datasets")
async def create_dataset(
    request: Request,
    payload: DatasetCreateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    # 权限：需要 knowledge:manage 或 admin
    try:
        request.app.state.dispatcher.rbac.require(user.roles, "knowledge:manage")
        return await svc.create_dataset(user, payload.model_dump())
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/datasets/{dataset_id}")
async def get_dataset(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.require_dataset_access(user, dataset_id, required="viewer")
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/knowledge/datasets/{dataset_id}")
async def update_dataset(
    dataset_id: str,
    patch: DatasetUpdateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.update_dataset(user, dataset_id, patch.model_dump(exclude_none=True))
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/knowledge/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        ok = await svc.delete_dataset(user, dataset_id)
        return {"status": "success" if ok else "not_found", "dataset_id": dataset_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/datasets/{dataset_id}/permissions")
async def list_dataset_permissions(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.list_dataset_permissions(user, dataset_id)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/knowledge/datasets/{dataset_id}/permissions")
async def grant_dataset_permission(
    dataset_id: str,
    payload: DatasetPermissionGrantSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        await svc.grant_dataset_permission(
            user,
            dataset_id,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            permission=payload.permission,
        )
        return {"status": "success"}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/knowledge/datasets/{dataset_id}/permissions")
async def revoke_dataset_permission(
    dataset_id: str,
    subject_type: str = Query(...),
    subject_id: str = Query(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        ok = await svc.revoke_dataset_permission(user, dataset_id, subject_type, subject_id)
        return {"status": "success" if ok else "not_found"}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/knowledge/{dataset_id}/documents")
async def list_documents(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.list_documents(user, dataset_id)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/text")
async def create_document_text(
    dataset_id: str,
    payload: DocumentCreateTextSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    try:
        doc = await svc.create_document_from_text(
            user,
            dataset_id,
            title=payload.title,
            content=payload.content,
            metadata=payload.metadata,
        )
        await worker.enqueue(dataset_id, doc["document_id"])
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/upload")
async def upload_document(
    dataset_id: str,
    file: UploadFile = File(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    try:
        content = await file.read()
        doc = await svc.create_document_from_upload(
            user,
            dataset_id,
            filename=file.filename or "upload",
            content_bytes=content,
            mime_type=file.content_type,
        )
        await worker.enqueue(dataset_id, doc["document_id"])
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/url")
async def create_document_url(
    dataset_id: str,
    payload: DocumentCreateUrlSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    try:
        doc = await svc.create_document_from_url(
            user,
            dataset_id,
            url=payload.url,
            title=payload.title,
            metadata=payload.metadata,
        )
        await worker.enqueue(dataset_id, doc["document_id"])
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/images")
async def upload_images(
    dataset_id: str,
    files: List[UploadFile] = File(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    """
    批量上传图片到知识库

    支持格式: JPEG, PNG, GIF, WebP, BMP
    最大大小: 3MB per image
    图片将通过VLM生成描述并进行多模态向量化
    """
    try:
        # Verify access
        await svc.require_dataset_access(user, dataset_id, required="editor")

        ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
        MAX_IMAGE_SIZE = 3 * 1024 * 1024  # 3MB

        # Magic bytes for image validation (prevents content-type spoofing)
        IMAGE_MAGIC_BYTES = {
            b'\xff\xd8\xff': "image/jpeg",      # JPEG
            b'\x89PNG\r\n\x1a\n': "image/png",  # PNG
            b'GIF87a': "image/gif",             # GIF87a
            b'GIF89a': "image/gif",             # GIF89a
            b'RIFF': "image/webp",              # WebP (starts with RIFF)
            b'BM': "image/bmp",                 # BMP
        }

        def validate_image_magic(data: bytes) -> bool:
            """Validate image by checking magic bytes."""
            for magic in IMAGE_MAGIC_BYTES:
                if data.startswith(magic):
                    return True
            # Special case for WebP: RIFF....WEBP
            if data[:4] == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
                return True
            return False

        results = []
        errors = []

        for file in files:
            # Validate content type
            if file.content_type not in ALLOWED_IMAGE_TYPES:
                errors.append({
                    "filename": file.filename,
                    "error": f"Unsupported image type: {file.content_type}"
                })
                continue

            # Read and validate size
            content = await file.read()
            if len(content) > MAX_IMAGE_SIZE:
                errors.append({
                    "filename": file.filename,
                    "error": f"Image too large: {len(content)} bytes (max {MAX_IMAGE_SIZE})"
                })
                continue

            # Validate magic bytes to prevent content-type spoofing
            if not validate_image_magic(content):
                errors.append({
                    "filename": file.filename,
                    "error": "Invalid image file: magic bytes validation failed"
                })
                continue

            # Create document
            doc = await svc.create_document_from_upload(
                user,
                dataset_id,
                filename=file.filename or "image",
                content_bytes=content,
                mime_type=file.content_type,
            )

            # Enqueue for processing (VLM description + multimodal embedding)
            await worker.enqueue(dataset_id, doc["document_id"])

            results.append({
                "document_id": doc["document_id"],
                "filename": file.filename,
                "size_bytes": len(content),
            })

        return {
            "uploaded": results,
            "success_count": len(results),
            "failed_count": len(errors),
            "errors": errors,
        }

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/images/{segment_id}")
async def get_image_segment(
    segment_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
    settings: Settings = Depends(get_settings),
):
    """
    Serve image content for a segment.

    Security:
    - Requires dataset viewer permission
    - For local file:// URLs: validates HMAC signature and expiry
    - Path traversal protection via base_path check
    """
    try:
        segment = await svc.db.get_segment(segment_id)
        if not segment or segment.get("content_type") != "image":
            raise HTTPException(status_code=404, detail="Image not found")

        dataset_id = str(segment.get("dataset_id") or "")
        if not dataset_id:
            raise HTTPException(status_code=404, detail="Image not found")

        await svc.require_dataset_access(user, dataset_id, required="viewer")

        image_url = segment.get("image_url") or ""
        if not image_url:
            raise HTTPException(status_code=404, detail="Image not found")

        if image_url.startswith("file://"):
            if not svc.image_storage_service:
                raise HTTPException(status_code=503, detail="Image storage service is not initialized.")

            # Verify URL signature for local files (security fix)
            signing_key = getattr(settings.confluence, "encryption_key", "") or ""
            if signing_key:
                is_valid, error_msg = verify_signed_url(image_url, signing_key)
                if not is_valid:
                    logger.warning(
                        f"Invalid image URL signature for segment {segment_id}: {error_msg}"
                    )
                    raise HTTPException(status_code=403, detail=f"Access denied: {error_msg}")

            # Get the unsigned URL path for file access
            unsigned_url = get_unsigned_url(image_url)
            base_path = Path(svc.image_storage_service.config.local_base_path).expanduser().resolve()
            parsed = urlparse(unsigned_url)
            file_path = Path(unquote(parsed.path)).expanduser().resolve()

            # Path traversal protection
            if base_path not in file_path.parents and file_path != base_path:
                logger.warning(
                    f"Path traversal attempt for segment {segment_id}: {file_path}"
                )
                raise HTTPException(status_code=403, detail="Access denied")
            if not file_path.exists():
                raise HTTPException(status_code=404, detail="Image not found")

            media_type = segment.get("image_media_type") or "application/octet-stream"
            return StreamingResponse(file_path.open("rb"), media_type=media_type)

        return RedirectResponse(image_url, status_code=307)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/knowledge/{dataset_id}/documents/{document_id}")
async def get_document(
    dataset_id: str,
    document_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.get_document(user, dataset_id, document_id)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/{document_id}/reindex")
async def reindex_document(
    dataset_id: str,
    document_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    try:
        await svc.require_dataset_access(user, dataset_id, required="editor")
        await worker.enqueue(dataset_id, document_id)
        return {"status": "queued", "document_id": document_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.delete("/knowledge/{dataset_id}/documents/{document_id}")
async def delete_document(
    dataset_id: str,
    document_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        ok = await svc.delete_document(user, dataset_id, document_id)
        return {"status": "success" if ok else "not_found", "document_id": document_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/{dataset_id}/segments")
async def list_segments(
    dataset_id: str,
    document_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.list_segments(user, dataset_id, document_id=document_id, q=q)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.put("/knowledge/{dataset_id}/segments/{segment_id}")
async def update_segment(
    dataset_id: str,
    segment_id: str,
    payload: SegmentUpdateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        return await svc.update_segment(user, dataset_id, segment_id, new_text=payload.text)
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/knowledge/{dataset_id}/segments/{segment_id}")
async def delete_segment(
    dataset_id: str,
    segment_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        ok = await svc.delete_segment(user, dataset_id, segment_id)
        return {"status": "success" if ok else "not_found", "segment_id": segment_id}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/retrieve")
async def retrieve(
    dataset_id: str,
    payload: RetrieveRequestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        # Use multimodal retrieval if include_associated_images is requested
        if payload.include_associated_images:
            results, meta = await svc.retrieve_with_images(
                user=user,
                dataset_id=dataset_id,
                query=payload.query,
                top_k=payload.top_k,
                mode=payload.mode,
                document_id=payload.document_id,
                dense_weight=payload.dense_weight,
                bm25_weight=payload.bm25_weight,
                fusion_method=payload.fusion_method,
                alpha=payload.alpha,
                score_threshold=payload.score_threshold,
                vector_top_k=payload.vector_top_k,
                keyword_top_k=payload.keyword_top_k,
                candidate_top_k=payload.candidate_top_k,
                keyword_candidate_k=payload.keyword_candidate_k,
                fusion=payload.fusion,
                rrf_k=payload.rrf_k,
                rrf_weights=payload.rrf_weights,
                rerank=payload.rerank,
                rerank_model=payload.rerank_model,
                rerank_top_n=payload.rerank_top_n,
                mmr=payload.mmr,
                mmr_lambda=payload.mmr_lambda,
                mmr_threshold=payload.mmr_threshold,
                include_images=payload.include_images,
                content_type_filter=payload.content_type_filter,
                multimodal_rerank=payload.multimodal_rerank,
                # Advanced multimodal parameters
                image_search_enabled=payload.image_search_enabled,
                vlm_rerank_weight=payload.vlm_rerank_weight,
                image_boost=payload.image_boost,
                image_score_threshold=payload.image_score_threshold,
                use_separate_thresholds=payload.use_separate_thresholds,
            )
        else:
            results, meta = await svc.retrieve(
                user=user,
                dataset_id=dataset_id,
                query=payload.query,
                top_k=payload.top_k,
                mode=payload.mode,
                document_id=payload.document_id,
                dense_weight=payload.dense_weight,
                bm25_weight=payload.bm25_weight,
                fusion_method=payload.fusion_method,
                alpha=payload.alpha,
                score_threshold=payload.score_threshold,
                vector_top_k=payload.vector_top_k,
                keyword_top_k=payload.keyword_top_k,
                candidate_top_k=payload.candidate_top_k,
                keyword_candidate_k=payload.keyword_candidate_k,
                fusion=payload.fusion,
                rrf_k=payload.rrf_k,
                rrf_weights=payload.rrf_weights,
                rerank=payload.rerank,
                rerank_model=payload.rerank_model,
                rerank_top_n=payload.rerank_top_n,
                mmr=payload.mmr,
                mmr_lambda=payload.mmr_lambda,
                mmr_threshold=payload.mmr_threshold,
            )

        # Build response with multimodal fields
        return {
            "results": [
                {
                    "segment_id": r.segment_id,
                    "document_id": r.document_id,
                    "score": r.score,
                    "text": r.text,
                    "metadata": r.metadata,
                    # P3: Multimodal fields
                    "content_type": getattr(r, "content_type", "text"),
                    "image_url": getattr(r, "image_url", None),
                    "vlm_description": getattr(r, "vlm_description", None),
                    "associated_images": getattr(r, "associated_images", []),
                }
                for r in results
            ],
            "metadata": meta,
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/retrieve_batch")
async def retrieve_batch(
    dataset_id: str,
    payload: BatchRetrieveRequestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Batch retrieval endpoint - parallel retrieval with multiple queries.

    Supports two formats:
    - queries: List of queries ["query1", "query2", "query3"]
    - query: Comma-separated queries "query1,query2,query3"

    Returns results grouped by query with execution time metrics.
    """
    try:
        # Parse queries from either format
        queries: list[str] = []
        if payload.queries:
            queries = payload.queries
        elif payload.query:
            # Support comma-separated queries
            queries = [q.strip() for q in payload.query.split(",") if q.strip()]

        if not queries:
            raise ValidationFailedError("No queries provided. Use 'queries' list or comma-separated 'query' string.")

        batch_results, meta = await svc.retrieve_batch(
            user=user,
            dataset_id=dataset_id,
            queries=queries,
            top_k=payload.top_k,
            mode=payload.mode,
            document_id=payload.document_id,
            dense_weight=payload.dense_weight,
            bm25_weight=payload.bm25_weight,
            fusion_method=payload.fusion_method,
            alpha=payload.alpha,
            score_threshold=payload.score_threshold,
            vector_top_k=payload.vector_top_k,
            keyword_top_k=payload.keyword_top_k,
            candidate_top_k=payload.candidate_top_k,
            keyword_candidate_k=payload.keyword_candidate_k,
            fusion=payload.fusion,
            rrf_k=payload.rrf_k,
            rrf_weights=payload.rrf_weights or {},
            rerank=payload.rerank,
            rerank_model=payload.rerank_model,
            rerank_top_n=payload.rerank_top_n,
            mmr=payload.mmr,
            mmr_lambda=payload.mmr_lambda,
            mmr_threshold=payload.mmr_threshold,
            include_images=payload.include_images,
            include_associated_images=payload.include_associated_images,
            max_parallel=payload.max_parallel,
            dedupe_results=payload.dedupe_results,
        )

        return {
            "batch_results": batch_results,
            "total_queries": meta.get("total_queries", len(queries)),
            "total_results": meta.get("total_results", 0),
            "execution_time_ms": meta.get("execution_time_ms", 0),
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/hit_test")
async def hit_test(
    dataset_id: str,
    payload: RetrieveRequestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Retrieve preview endpoint for debugging (includes raw scores in metadata)."""
    try:
        payload.mode = payload.mode or "hybrid"
        results, meta = await svc.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=payload.query,
            top_k=payload.top_k,
            mode=payload.mode,
            document_id=payload.document_id,
            dense_weight=payload.dense_weight,
            bm25_weight=payload.bm25_weight,
            fusion_method=payload.fusion_method,
            alpha=payload.alpha,
            score_threshold=payload.score_threshold,
            vector_top_k=payload.vector_top_k,
            keyword_top_k=payload.keyword_top_k,
            candidate_top_k=payload.candidate_top_k,
            keyword_candidate_k=payload.keyword_candidate_k,
            fusion=payload.fusion,
            rrf_k=payload.rrf_k,
            rrf_weights=payload.rrf_weights or {},
            rerank=payload.rerank,
            rerank_model=payload.rerank_model,
            rerank_top_n=payload.rerank_top_n,
            mmr=payload.mmr,
            mmr_lambda=payload.mmr_lambda,
            mmr_threshold=payload.mmr_threshold,
        )
        return {
            "results": [
                {
                    "segment_id": r.segment_id,
                    "document_id": r.document_id,
                    "score": r.score,
                    "text": r.text,
                    "metadata": r.metadata,
                }
                for r in results
            ],
            "metadata": meta,
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        # Catch any unexpected errors and return a meaningful response
        import traceback
        error_detail = str(exc)
        traceback_str = traceback.format_exc()
        return {
            "results": [],
            "metadata": {
                "error": error_detail,
                "traceback": traceback_str if "DEBUG" in str(svc.settings.log_level).upper() else None,
                "mode": payload.mode,
                "top_k": payload.top_k,
            },
        }


# ============================================================
# Document Enable/Disable/Archive Endpoints (Dify-style)
# ============================================================

@router.patch("/knowledge/{dataset_id}/documents/{document_id}/status")
async def update_document_status(
    dataset_id: str,
    document_id: str,
    payload: DocumentEnableDisableSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Enable or disable a document."""
    try:
        doc = await svc.set_document_enabled(user, dataset_id, document_id, payload.enabled)
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/knowledge/{dataset_id}/documents/{document_id}/archive")
async def archive_document(
    dataset_id: str,
    document_id: str,
    payload: DocumentArchiveSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Archive or unarchive a document."""
    try:
        doc = await svc.set_document_archived(
            user, dataset_id, document_id, payload.archived, payload.reason
        )
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/knowledge/{dataset_id}/documents/{document_id}")
async def update_document(
    dataset_id: str,
    document_id: str,
    payload: DocumentUpdateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Update document metadata."""
    try:
        doc = await svc.update_document(user, dataset_id, document_id, payload.model_dump(exclude_none=True))
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))



@router.post("/knowledge/preview")
async def preview_chunking_generic(
    payload: ChunkPreviewRequestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Generic preview endpoint (no dataset context required).
    """
    try:
        # Use a dummy dataset ID since we don't have one yet
        chunks = await svc.preview_chunking(
            user, 
            "temp_preview", 
            text=payload.text,
            config=payload.config.model_dump() if payload.config else None
        )
        return {"chunks": chunks, "total_chunks": len(chunks)}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/chunk/preview")
async def preview_chunking(
    dataset_id: str,
    payload: ChunkPreviewRequestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Preview chunking results for a given text and configuration.
    Does not save anything. useful for testing chunking strategies.
    """
    try:
        chunks = await svc.preview_chunking(
            user, 
            dataset_id, 
            text=payload.text,
            config=payload.config.model_dump() if payload.config else None
        )
        return {"chunks": chunks, "total_chunks": len(chunks)}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

# ============================================================
# Batch Operations (Dify-style)
# ============================================================

@router.post("/knowledge/{dataset_id}/documents/batch")
async def batch_create_documents(
    dataset_id: str,
    payload: DocumentBatchCreateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    """Batch create documents from text."""
    try:
        results = await svc.batch_create_documents(
            user,
            dataset_id,
            documents=payload.documents,
            process_rule=payload.process_rule.model_dump() if payload.process_rule else None,
            batch_name=payload.batch_name,
        )
        # Enqueue all for processing
        for doc in results.get("documents", []):
            await worker.enqueue(dataset_id, doc["document_id"])
        return results
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/batch-reindex")
async def batch_reindex_documents(
    dataset_id: str,
    payload: BatchReindexSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    """Batch reindex documents."""
    try:
        await svc.require_dataset_access(user, dataset_id, required="editor")
        
        if payload.all_documents:
            docs = await svc.list_documents(user, dataset_id)
            doc_ids = [d["document_id"] for d in docs]
        else:
            doc_ids = payload.document_ids
        
        for doc_id in doc_ids:
            await worker.enqueue(dataset_id, doc_id)
        
        return {"status": "queued", "document_count": len(doc_ids)}
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/batch-delete")
async def batch_delete_documents(
    dataset_id: str,
    payload: BatchDeleteSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Batch delete documents."""
    try:
        results = await svc.batch_delete_documents(user, dataset_id, payload.document_ids)
        return results
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# Segment Enable/Disable Endpoints
# ============================================================

@router.patch("/knowledge/{dataset_id}/segments/{segment_id}/status")
async def update_segment_status(
    dataset_id: str,
    segment_id: str,
    payload: SegmentEnableDisableSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Enable or disable a segment."""
    try:
        seg = await svc.set_segment_enabled(user, dataset_id, segment_id, payload.enabled)
        return seg
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/{document_id}/segments")
async def create_segment(
    dataset_id: str,
    document_id: str,
    payload: SegmentCreateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Create a new segment manually."""
    try:
        seg = await svc.create_segment(
            user,
            dataset_id,
            document_id,
            content=payload.content,
            answer=payload.answer,
            keywords=payload.keywords,
        )
        return seg
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# Statistics Endpoints
# ============================================================

@router.get("/knowledge/{dataset_id}/statistics")
async def get_dataset_statistics(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Get dataset statistics (document count, segment count, etc.)."""
    try:
        stats = await svc.get_dataset_statistics(user, dataset_id)
        return stats
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/{dataset_id}/debug")
async def debug_dataset(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Debug endpoint to check dataset status."""
    try:
        dataset = await svc.require_dataset_access(user, dataset_id, required="viewer")
        stats = await svc.get_dataset_statistics(user, dataset_id)
        
        # Sample a few segments to verify
        sample_segments = await svc.db.list_segments(dataset_id=dataset_id, limit=3, offset=0)
        
        return {
            "dataset": {
                "id": dataset_id,
                "name": dataset.get("name"),
                "embedding_provider": dataset.get("embedding_provider"),
                "embedding_model": dataset.get("embedding_model"),
                "embedding_dimension": dataset.get("embedding_dimension"),
                "collection_name": dataset.get("collection_name"),
            },
            "statistics": stats,
            "sample_segments": [
                {
                    "segment_id": s.get("segment_id"),
                    "document_id": s.get("document_id"),
                    "text_preview": (s.get("text") or "")[:100] + "..." if s.get("text") else None,
                    "token_count": s.get("token_count"),
                    "vector_id": s.get("vector_id"),
                }
                for s in sample_segments
            ],
            "has_segments": len(sample_segments) > 0,
            "has_collection": bool(dataset.get("collection_name")),
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/knowledge/{dataset_id}/documents/{document_id}/statistics")
async def get_document_statistics(
    dataset_id: str,
    document_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Get document statistics."""
    try:
        stats = await svc.get_document_statistics(user, dataset_id, document_id)
        return stats
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# QA Testing Endpoints
# ============================================================

@router.post("/knowledge/{dataset_id}/qa")
async def qa_query(
    request: Request,
    dataset_id: str,
    payload: QAQuerySchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Execute a QA query: retrieve → context → LLM answer.
    
    This endpoint provides a complete RAG flow for testing retrieval quality.
    """
    try:
        from ...services.knowledge.qa_service import QAService, LLMConfig, LLMProvider
        
        # Build LLM config
        llm_config = None
        if payload.llm_config:
            llm_config = LLMConfig.from_dict(payload.llm_config.model_dump())
        
        # Create QA service
        qa_service = QAService(svc, llm_config)
        
        try:
            result = await qa_service.query(
                user_context=user,
                dataset_id=dataset_id,
                query=payload.query,
                top_k=payload.top_k,
                mode=payload.mode,
                document_id=payload.document_id,
                rerank=payload.rerank,
                rerank_top_n=payload.rerank_top_n,
                mmr=payload.mmr,
                mmr_lambda=payload.mmr_lambda,
                fusion_method=payload.fusion_method,
                dense_weight=payload.dense_weight,
                bm25_weight=payload.bm25_weight,
                score_threshold=payload.score_threshold,
                include_raw_results=payload.include_raw_results,
            )
            
            return {
                "query": result.query,
                "answer": result.answer,
                "context_segments": result.context_segments,
                "retrieval_metadata": result.retrieval_metadata,
                "timing": {
                    "retrieval_ms": result.retrieval_time_ms,
                    "llm_ms": result.llm_time_ms,
                    "total_ms": result.total_time_ms,
                },
                "model": result.model,
                "tokens_used": result.tokens_used,
            }
        finally:
            await qa_service.close()
            
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"QA query failed: {str(exc)}")


@router.post("/knowledge/{dataset_id}/qa/stream")
async def qa_query_stream(
    request: Request,
    dataset_id: str,
    payload: QAQuerySchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Stream QA query: retrieve → stream LLM answer.
    """
    from ...services.knowledge.qa_service import QAService, LLMConfig

    llm_config = None
    if payload.llm_config:
        llm_config = LLMConfig.from_dict(payload.llm_config.model_dump())

    qa_service = QAService(svc, llm_config)

    async def event_generator():
        try:
            async for event in qa_service.query_stream(
                user_context=user,
                dataset_id=dataset_id,
                query=payload.query,
                top_k=payload.top_k,
                mode=payload.mode,
                document_id=payload.document_id,
                rerank=payload.rerank,
                rerank_top_n=payload.rerank_top_n,
                mmr=payload.mmr,
                mmr_lambda=payload.mmr_lambda,
                fusion_method=payload.fusion_method,
                dense_weight=payload.dense_weight,
                bm25_weight=payload.bm25_weight,
                score_threshold=payload.score_threshold,
                include_raw_results=payload.include_raw_results,
            ):
                payload_json = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload_json}\n\n"
        except PermissionDeniedError as exc:
            payload_json = json.dumps({"event": "error", "data": {"message": str(exc)}}, ensure_ascii=False)
            yield f"data: {payload_json}\n\n"
        except ValidationFailedError as exc:
            payload_json = json.dumps({"event": "error", "data": {"message": str(exc)}}, ensure_ascii=False)
            yield f"data: {payload_json}\n\n"
        except Exception as exc:
            payload_json = json.dumps({"event": "error", "data": {"message": str(exc)}}, ensure_ascii=False)
            yield f"data: {payload_json}\n\n"
        finally:
            await qa_service.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/knowledge/{dataset_id}/qa/batch")
async def qa_batch_test(
    request: Request,
    dataset_id: str,
    payload: QABatchTestSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Run batch QA tests for evaluation.
    
    Executes multiple test cases and returns aggregated results.
    """
    try:
        from ...services.knowledge.qa_service import (
            QAService, 
            LLMConfig, 
            QATestCase,
        )
        
        # Build LLM config
        llm_config = None
        if payload.llm_config:
            llm_config = LLMConfig.from_dict(payload.llm_config.model_dump())
        
        # Create QA service
        qa_service = QAService(svc, llm_config)
        
        try:
            # Convert test cases
            test_cases = [
                QATestCase(
                    query=tc.query,
                    expected_answer=tc.expected_answer,
                    expected_segments=tc.expected_segments,
                )
                for tc in payload.test_cases
            ]
            
            # Run batch test
            results = await qa_service.run_test_batch(
                user_context=user,
                dataset_id=dataset_id,
                test_cases=test_cases,
                top_k=payload.top_k,
                mode=payload.mode,
                rerank=payload.rerank,
                mmr=payload.mmr,
            )
            
            # Aggregate results
            summary = qa_service.aggregate_test_results(results)
            
            return {
                "results": [r.to_dict() for r in results],
                "summary": summary,
            }
        finally:
            await qa_service.close()
            
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Batch QA test failed: {str(exc)}")


# ============================================================
# Configuration Endpoints
# ============================================================

@router.get("/knowledge/{dataset_id}/config")
async def get_dataset_config(
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Get dataset chunking and retrieval configuration."""
    try:
        dataset = await svc.require_dataset_access(user, dataset_id, required="viewer")
        
        # Extract configurations from index_config
        index_config = dataset.get("index_config", {}) or {}
        
        # Also get statistics
        try:
            stats = await svc.get_dataset_statistics(user, dataset_id)
        except Exception:
            stats = {}
        
        return {
            "dataset_id": dataset_id,
            "chunking": index_config.get("chunking", {
                "mode": "automatic",
                "chunk_size": 500,
                "chunk_overlap": 50,
            }),
            "retrieval": index_config.get("retrieval", {
                "mode": "hybrid",
                "top_k": 5,
                "rerank": {"enabled": False},
                "mmr": {"enabled": False},
            }),
            "embedding": {
                "provider": dataset.get("embedding_provider"),
                "model": dataset.get("embedding_model"),
                "dimension": dataset.get("embedding_dimension"),
                "collection_name": dataset.get("collection_name"),
            },
            "statistics": stats,
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/knowledge/{dataset_id}/config")
async def update_dataset_config(
    dataset_id: str,
    payload: DatasetConfigUpdateSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """Update dataset chunking and retrieval configuration."""
    try:
        dataset = await svc.require_dataset_access(user, dataset_id, required="owner")
        
        # Get current config
        index_config = dict(dataset.get("index_config", {}) or {})
        
        # Update chunking config
        if payload.chunking_config:
            index_config["chunking"] = payload.chunking_config.model_dump(exclude_none=True)
        
        # Update retrieval config
        if payload.retrieval_config:
            retrieval = payload.retrieval_config.model_dump(exclude_none=True)
            
            # Convert flat structure to nested for compatibility
            index_config["retrieval"] = {
                "mode": retrieval.get("mode", "hybrid"),
                "top_k": retrieval.get("top_k", 5),
                "score_threshold": retrieval.get("score_threshold"),
                "vector_top_k": retrieval.get("vector_top_k", 20),
                "keyword_top_k": retrieval.get("keyword_top_k", 20),
                "fusion": retrieval.get("fusion_strategy", "rrf"),
                "rrf_k": retrieval.get("rrf_k", 60),
                "alpha": retrieval.get("alpha", 0.75),
                "rerank": {
                    "enabled": retrieval.get("rerank_enabled", False),
                    "model": retrieval.get("rerank_model", "gte-rerank"),
                    "top_n": retrieval.get("rerank_top_n"),
                },
                "mmr": {
                    "enabled": retrieval.get("mmr_enabled", False),
                    "lambda": retrieval.get("mmr_lambda", 0.5),
                },
            }
        
        # Save updated config
        updated = await svc.update_dataset(user, dataset_id, {"index_config": index_config})
        
        return {
            "status": "success",
            "dataset_id": dataset_id,
            "index_config": updated.get("index_config"),
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ============================================================
# Chunk Preview Endpoint
# ============================================================

class ChunkPreviewRequest(BaseModel):
    """Request for chunk preview."""
    text: str = Field(..., description="Text to chunk")
    chunking_config: Optional[Dict[str, Any]] = Field(
        default=None, 
        description="Chunking configuration. Uses dataset defaults if not provided."
    )

class ChunkPreviewItem(BaseModel):
    """Single chunk preview item."""
    index: int
    text: str
    char_count: int
    token_count: int
    word_count: int

class ChunkPreviewResponse(BaseModel):
    """Response for chunk preview."""
    total_chunks: int
    chunks: List[ChunkPreviewItem]
    config_used: Dict[str, Any]


@router.post("/knowledge/{dataset_id}/preview-chunks")
async def preview_chunks(
    dataset_id: str,
    payload: ChunkPreviewRequest = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Preview how text would be chunked with the given configuration.

    This is useful for testing chunking settings before processing documents.
    """
    try:
        dataset = await svc.require_dataset_access(user, dataset_id, required="viewer")

        # Import chunking module
        from src.services.knowledge.chunking import (
            ChunkingConfig,
            process_document,
            flatten_chunks
        )

        # Get chunking config - use provided or fall back to dataset defaults
        if payload.chunking_config:
            config_dict = payload.chunking_config
        else:
            index_config = dataset.get("index_config", {}) or {}
            config_dict = index_config.get("chunking", {})

        # Parse config
        config = ChunkingConfig.from_dict(config_dict)

        # Process text
        chunks = process_document(payload.text, config)
        flat_chunks = flatten_chunks(chunks)

        # Format response
        preview_items = [
            ChunkPreviewItem(
                index=i,
                text=chunk.text,
                char_count=chunk.char_count,
                token_count=chunk.token_count,
                word_count=chunk.word_count,
            )
            for i, chunk in enumerate(flat_chunks)
        ]

        return ChunkPreviewResponse(
            total_chunks=len(preview_items),
            chunks=preview_items,
            config_used=config.to_dict(),
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Chunking error: {exc}")


@router.get("/knowledge/{dataset_id}/sources", summary="Get dataset source statistics")
async def get_dataset_sources(
    request: Request,
    dataset_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Get dataset source statistics including file uploads, URL imports, and Confluence sync.

    Returns counts by source type and details of any Confluence bindings.
    """
    try:
        # Verify access
        await svc.require_dataset_access(user, dataset_id, required="viewer")

        # Get document statistics by source_type using database
        docs = await svc.list_documents(user, dataset_id)
        source_counts: Dict[str, int] = {}
        for doc in docs:
            source_type = doc.get("source_type") or "upload"
            source_counts[source_type] = source_counts.get(source_type, 0) + 1

        # Get Confluence bindings if service is available
        confluence_bindings = []
        confluence_svc = getattr(request.app.state, "confluence_sync_service", None)
        if confluence_svc:
            try:
                bindings = await confluence_svc.list_bindings(user, dataset_id=dataset_id)
                confluence_bindings = [
                    {
                        "binding_id": b.get("binding_id"),
                        "space_name": b.get("space_name"),
                        "page_count": b.get("synced_page_count") or 0,
                        "status": b.get("status"),
                    }
                    for b in bindings
                ]
            except Exception as e:
                logger.warning(f"Failed to get Confluence bindings for dataset {dataset_id}: {e}")

        return {
            "file_uploads": {
                "count": source_counts.get("upload", 0),
            },
            "url_imports": {
                "count": source_counts.get("url", 0),
            },
            "confluence_bindings": confluence_bindings,
            "total_documents": sum(source_counts.values()),
        }
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# =============================================================================
# Maintenance Endpoints (Internal Use Only)
# =============================================================================

@router.post("/knowledge/{dataset_id}/maintenance/dedupe")
async def dedupe_segments(
    dataset_id: str,
    request: Request,
    dry_run: bool = Query(default=True, description="If true, only report duplicates without deleting"),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Remove duplicate segments from a dataset.

    Duplicates are identified by content_hash. The oldest segment is kept,
    newer duplicates are deleted from both database and vector store.

    Use dry_run=true to preview what would be deleted.

    Internal maintenance: Use X-Internal-Maintenance: true header to bypass permission check.
    """
    from collections import defaultdict

    try:
        # Allow internal maintenance calls to bypass permission check
        internal_header = request.headers.get("X-Internal-Maintenance", "").lower()
        if internal_header == "true":
            dataset = await svc.db.get_dataset(dataset_id)
            if not dataset:
                raise ValidationFailedError(f"dataset {dataset_id} not found")
        else:
            # Require admin/owner access for maintenance operations
            dataset = await svc.require_dataset_access(user, dataset_id, required="owner")

        collection_name = dataset.get("collection_name")

        # Get all segments grouped by content_hash
        segments = await svc.db.list_segments(dataset_id, limit=10000)

        # Group by content_hash
        hash_to_segments = defaultdict(list)
        for seg in segments:
            content_hash = seg.get("content_hash") or ""
            if content_hash:
                hash_to_segments[content_hash].append(seg)

        # Find duplicates (keep oldest, delete rest)
        duplicates_to_delete = []
        for content_hash, segs in hash_to_segments.items():
            if len(segs) > 1:
                # Sort by created_at, keep oldest
                segs.sort(key=lambda x: x.get("created_at") or "")
                duplicates_to_delete.extend(segs[1:])

        result = {
            "dataset_id": dataset_id,
            "total_segments": len(segments),
            "unique_content": len(hash_to_segments),
            "duplicates_found": len(duplicates_to_delete),
            "dry_run": dry_run,
        }

        if dry_run:
            # Return preview of duplicates
            result["duplicates_preview"] = [
                {
                    "segment_id": seg.get("segment_id"),
                    "content_preview": (seg.get("content") or "")[:100],
                }
                for seg in duplicates_to_delete[:10]
            ]
            return result

        # Actually delete duplicates
        deleted_count = 0
        errors = []

        for seg in duplicates_to_delete:
            seg_id = seg.get("segment_id")
            try:
                # Delete from vector store
                if collection_name:
                    vector_id = seg.get("vector_id") or seg_id
                    try:
                        await svc.vector_store.delete_points(collection_name, [vector_id])
                    except Exception:
                        pass

                # Delete from database
                ok = await svc.db.delete_segment(seg_id)
                if ok:
                    deleted_count += 1
            except Exception as e:
                errors.append({"segment_id": seg_id, "error": str(e)})

        result["deleted_count"] = deleted_count
        if errors:
            result["errors"] = errors[:10]

        return result

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
