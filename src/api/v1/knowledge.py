from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
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
    DatasetDeleteSchema,
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
        dataset = await svc.require_dataset_access(user, dataset_id, required="viewer")
        return svc.sanitize_dataset_for_response(dataset)
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
    payload: DatasetDeleteSchema = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    try:
        ok = await svc.delete_dataset(
            user,
            dataset_id,
            password=payload.password,
            reason=payload.reason,
        )
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
    processing_mode: str = Form("auto"),  # auto | text_only | scanned | multimodal
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    """
    Upload a document to the knowledge base.
    
    Args:
        dataset_id: Target dataset ID
        file: Document file (PDF, DOCX, TXT, etc.)
        processing_mode: Processing mode - one of:
            - auto: Automatic detection (default) - intelligently detects document type
            - text_only: Traditional OCR + text embedding
            - scanned: Page-as-Image vision embedding (for scanned PDFs)
            - multimodal: Combined text + image embedding
    """
    try:
        content = await file.read()
        logger.info(
            "Upload started: file=%s, size=%d, dataset=%s, mode=%s", 
            file.filename, len(content), dataset_id, processing_mode
        )
        doc = await svc.create_document_from_upload(
            user,
            dataset_id,
            filename=file.filename or "upload",
            content_bytes=content,
            mime_type=file.content_type,
            processing_mode=processing_mode,
        )
        logger.info("Document created: id=%s, enqueueing for ingestion...", doc["document_id"])
        await worker.enqueue(dataset_id, doc["document_id"])
        logger.info("Document enqueued: id=%s, worker queue size ~%d", doc["document_id"], worker.queue.qsize())
        return doc
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/batch-upload")
async def batch_upload_documents(
    dataset_id: str,
    files: List[UploadFile] = File(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
    settings: Settings = Depends(get_settings),
):
    """
    批量上传文档到知识库（支持并行处理）
    
    支持格式: PDF, DOCX, TXT, MD, HTML
    最大文件数: 50
    最大单文件大小: 由系统配置决定
    
    使用流式写入临时文件，避免大文件占用过多内存。
    
    返回:
        {
            "batch_id": "uuid",
            "total": 5,
            "accepted": 5,
            "rejected": 0,
            "documents": [...],
            "errors": []
        }
    """
    import uuid as uuid_lib
    import tempfile
    import aiofiles
    import os
    
    try:
        await svc.require_dataset_access(user, dataset_id, required="editor")
        
        MAX_FILES = 50
        CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
        ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".html"}
        
        if len(files) > MAX_FILES:
            raise ValidationFailedError(f"Maximum {MAX_FILES} files allowed per batch")
        
        if not files:
            raise ValidationFailedError("No files provided")
        
        batch_id = str(uuid_lib.uuid4())
        documents = []
        errors = []
        
        for file in files:
            filename = file.filename or "unknown"
            ext = Path(filename).suffix.lower()
            
            # Validate extension
            if ext not in ALLOWED_EXTENSIONS:
                errors.append({
                    "filename": filename,
                    "error": f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
                })
                continue
            
            temp_path = None
            try:
                # Stream file to temp location to avoid memory exhaustion
                # Use tempfile with delete=False so we control cleanup
                with tempfile.NamedTemporaryFile(
                    delete=False, 
                    suffix=ext,
                    prefix=f"batch_{batch_id[:8]}_"
                ) as tmp:
                    temp_path = tmp.name
                
                # Stream write using aiofiles
                async with aiofiles.open(temp_path, 'wb') as out_file:
                    while True:
                        chunk = await file.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        await out_file.write(chunk)
                
                # Read back for processing (file is now on disk, not all in memory at once)
                async with aiofiles.open(temp_path, 'rb') as in_file:
                    content = await in_file.read()
                
                # Create document record
                doc = await svc.create_document_from_upload(
                    user,
                    dataset_id,
                    filename=filename,
                    content_bytes=content,
                    mime_type=file.content_type,
                )
                
                # Add batch metadata
                doc["batch_id"] = batch_id
                documents.append(doc)
                
            except Exception as e:
                errors.append({
                    "filename": filename,
                    "error": str(e)
                })
            finally:
                # Clean up temp file
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
        
        # Enqueue all documents for parallel processing
        # Worker will process them based on document_worker_concurrency setting
        for doc in documents:
            await worker.enqueue(dataset_id, doc["document_id"])
        
        return {
            "batch_id": batch_id,
            "total": len(files),
            "accepted": len(documents),
            "rejected": len(errors),
            "documents": documents,
            "errors": errors,
        }
        
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
        # Update status immediately so UI shows "解析中" after refetch instead of staying "已上传"
        try:
            await svc.db.update_document_status(document_id, status="parsing", progress=0)
        except Exception as e:
            logger.warning("Could not update document status after reindex enqueue: %s", e)
        logger.info("Reindex queued for document %s (dataset=%s)", document_id, dataset_id)
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
        # Use hierarchical retrieval if enabled
        if payload.hierarchical:
            from ...services.knowledge.hierarchical_retriever import hierarchical_retrieve
            
            results, meta = await hierarchical_retrieve(
                query=payload.query,
                dataset_id=dataset_id,
                vector_store=svc.vector_store,
                embedder=svc.embedder,
                database=svc.db,
                top_k=payload.top_k,
                strategy=payload.hierarchical_strategy.value,
                l1_top_k=payload.l1_top_k,
                l2_top_k=payload.l2_top_k,
                include_context=payload.include_context,
                score_threshold=payload.score_threshold,
            )
            
            # Build hierarchical response
            return {
                "results": [
                    {
                        "segment_id": r.segment_id,
                        "document_id": r.document_id,
                        "score": r.score,
                        "text": r.text,
                        "level": r.level,
                        "metadata": r.metadata,
                        "parent_context": r.parent_context,
                        "document_summary": r.document_summary,
                    }
                    for r in results
                ],
                "metadata": {
                    "strategy": meta.strategy,
                    "l1_candidates": meta.l1_candidates,
                    "l2_candidates": meta.l2_candidates,
                    "l3_results": meta.l3_results,
                    "total_time_ms": meta.total_time_ms,
                    "filtered_documents": meta.filtered_documents,
                },
            }
        
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
                source_type_filter=payload.source_type_filter,
                language_filter=payload.language_filter,
                multi_query=payload.multi_query,
                authority_sort=payload.authority_sort,
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
                source_type_filter=payload.source_type_filter,
                language_filter=payload.language_filter,
                multi_query=payload.multi_query,
                authority_sort=payload.authority_sort,
            )

        # Build response with multimodal + Islamic traceability fields
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
                    # Islamic knowledge traceability fields
                    "source_type": (r.metadata or {}).get("source_type"),
                    "citation_text": (r.metadata or {}).get("citation_text"),
                    "source_reference": (r.metadata or {}).get("source_reference", {}),
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

            # Extract nested objects sent by frontend
            fusion_cfg = retrieval.get("fusion", {}) or {}
            rerank_cfg = retrieval.get("rerank", {}) or {}
            mmr_cfg = retrieval.get("mmr", {}) or {}

            index_config["retrieval"] = {
                "mode": retrieval.get("mode", "hybrid"),
                "top_k": retrieval.get("top_k", 5),
                "score_threshold": retrieval.get("score_threshold"),
                "vector_top_k": retrieval.get("vector_top_k", 20),
                "keyword_top_k": retrieval.get("keyword_top_k", 20),
                "fusion": {
                    "strategy": fusion_cfg.get("strategy", "rrf"),
                    "alpha": fusion_cfg.get("alpha", 0.7),
                    "rrf_k": fusion_cfg.get("rrf_k", 60),
                },
                "rerank": {
                    "enabled": rerank_cfg.get("enabled", False),
                    "model": rerank_cfg.get("model", "gte-rerank"),
                    "top_n": rerank_cfg.get("top_n"),
                },
                "mmr": {
                    "enabled": mmr_cfg.get("enabled", False),
                    "lambda": mmr_cfg.get("lambda", 0.5),
                },
            }
        
        # Build dataset updates
        dataset_updates: Dict[str, Any] = {"index_config": index_config}
        
        # Handle embedding config updates if provided
        # Note: embedding changes require dimension check to avoid breaking existing vectors
        embedding_provider = getattr(payload, 'embedding_provider', None)
        embedding_model = getattr(payload, 'embedding_model', None)
        embedding_dimension = getattr(payload, 'embedding_dimension', None)
        
        # Validate embedding dimension changes don't break existing vectors
        current_dimension = dataset.get("embedding_dimension")
        if embedding_dimension is not None and embedding_dimension != current_dimension:
            # Check if dataset has existing segments
            stats = await svc.get_dataset_statistics(user, dataset_id)
            segment_count = stats.get("segment_count", 0)
            if segment_count > 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot change embedding dimension when {segment_count} segments exist. "
                           "Please create a new dataset or delete all existing documents first."
                )
        
        if embedding_provider is not None:
            dataset_updates["embedding_provider"] = embedding_provider
        if embedding_model is not None:
            dataset_updates["embedding_model"] = embedding_model
        if embedding_dimension is not None:
            dataset_updates["embedding_dimension"] = embedding_dimension
        
        # Save updated config
        updated = await svc.update_dataset(user, dataset_id, dataset_updates)
        
        return {
            "status": "success",
            "dataset_id": dataset_id,
            "index_config": updated.get("index_config"),
            "embedding": {
                "provider": updated.get("embedding_provider"),
                "model": updated.get("embedding_model"),
                "dimension": updated.get("embedding_dimension"),
            },
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
            flatten_chunks,
            merge_small_chunks,
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
        flat_chunks = merge_small_chunks(
            flat_chunks,
            min_size=config.min_chunk_size,
            max_size=config.max_chunk_size,
            min_tokens=config.min_chunk_tokens if config.use_token_count else None,
            max_tokens=config.max_chunk_tokens if config.use_token_count else None,
        )

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
    settings: Settings = Depends(get_settings),
):
    """
    Remove duplicate segments from a dataset.

    Duplicates are identified by content_hash. The oldest segment is kept,
    newer duplicates are deleted from both database and vector store.

    Use dry_run=true to preview what would be deleted.

    Requires owner access to the dataset.
    """
    from collections import defaultdict

    try:
        # Always require owner access for maintenance operations
        # Security: No bypass allowed - maintenance operations should be properly authenticated
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


# ============================================================
# Document Version Control APIs
# ============================================================

class VersionListResponse(BaseModel):
    """Version list response schema"""
    versions: List[Dict[str, Any]]
    total: int
    current_version: Optional[int] = None


class VersionCompareResponse(BaseModel):
    """Version compare response schema"""
    from_version: int
    to_version: int
    diff: List[Dict[str, Any]]
    stats: Dict[str, int]


class VersionRestoreRequest(BaseModel):
    """Version restore request schema"""
    reason: Optional[str] = Field(None, description="Reason for restoring this version")


@router.get("/knowledge/{dataset_id}/documents/{document_id}/versions")
async def list_document_versions(
    dataset_id: str,
    document_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    List version history for a document.

    Returns a list of versions without full content (for performance).
    Use GET .../versions/{version_number} to get full content.
    """
    try:
        # Verify access
        await svc.require_dataset_access(user, dataset_id, required="viewer")

        # Get document to verify it exists and get current version
        doc = await svc.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("Document not found")

        # Get versions
        versions = await svc.db.list_document_versions(document_id, limit, offset)
        total = await svc.db.get_document_version_count(document_id)

        return VersionListResponse(
            versions=versions,
            total=total,
            current_version=doc.get("current_version", 1),
        )

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/knowledge/{dataset_id}/documents/{document_id}/versions/{version_number}")
async def get_document_version(
    dataset_id: str,
    document_id: str,
    version_number: int,
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Get a specific version of a document with full content.
    """
    try:
        # Verify access
        await svc.require_dataset_access(user, dataset_id, required="viewer")

        # Get document to verify it exists
        doc = await svc.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("Document not found")

        # Get specific version
        version = await svc.db.get_document_version(document_id, version_number)
        if not version:
            raise ValidationFailedError(f"Version {version_number} not found")

        return version

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/knowledge/{dataset_id}/documents/{document_id}/versions/compare")
async def compare_document_versions(
    dataset_id: str,
    document_id: str,
    from_version: int = Query(..., description="Version to compare from"),
    to_version: int = Query(..., description="Version to compare to"),
    svc: KnowledgeService = Depends(get_knowledge_service),
    user: UserContext = Depends(get_user_context),
):
    """
    Compare two versions of a document.

    Returns a unified diff showing additions, deletions, and changes.
    """
    import difflib

    try:
        # Verify access
        await svc.require_dataset_access(user, dataset_id, required="viewer")

        # Get document to verify it exists
        doc = await svc.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("Document not found")

        # Get both versions
        version_from = await svc.db.get_document_version(document_id, from_version)
        version_to = await svc.db.get_document_version(document_id, to_version)

        if not version_from:
            raise ValidationFailedError(f"Version {from_version} not found")
        if not version_to:
            raise ValidationFailedError(f"Version {to_version} not found")

        # Get content
        content_from = (version_from.get("content") or "").splitlines(keepends=True)
        content_to = (version_to.get("content") or "").splitlines(keepends=True)

        # Generate unified diff
        diff_lines = list(difflib.unified_diff(
            content_from,
            content_to,
            fromfile=f"Version {from_version}",
            tofile=f"Version {to_version}",
            lineterm=""
        ))

        # Parse diff into structured format
        diff_items = []
        additions = 0
        deletions = 0

        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                continue
            elif line.startswith("+"):
                diff_items.append({"type": "insert", "content": line[1:]})
                additions += 1
            elif line.startswith("-"):
                diff_items.append({"type": "delete", "content": line[1:]})
                deletions += 1
            else:
                diff_items.append({"type": "equal", "content": line})

        return VersionCompareResponse(
            from_version=from_version,
            to_version=to_version,
            diff=diff_items[:500],  # Limit to 500 lines
            stats={
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
            }
        )

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/knowledge/{dataset_id}/documents/{document_id}/versions/{version_number}/restore")
async def restore_document_version(
    dataset_id: str,
    document_id: str,
    version_number: int,
    payload: VersionRestoreRequest = Body(default=VersionRestoreRequest()),
    svc: KnowledgeService = Depends(get_knowledge_service),
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
    user: UserContext = Depends(get_user_context),
):
    """
    Restore a document to a specific version.

    This creates a new version with the content from the specified version,
    then re-indexes the document.
    """
    import hashlib

    try:
        # Verify access (need editor permission to restore)
        await svc.require_dataset_access(user, dataset_id, required="editor")

        # Get document to verify it exists
        doc = await svc.db.get_document(document_id)
        if not doc or str(doc.get("dataset_id")) != dataset_id:
            raise ValidationFailedError("Document not found")

        # Get the version to restore
        version_to_restore = await svc.db.get_document_version(document_id, version_number)
        if not version_to_restore:
            raise ValidationFailedError(f"Version {version_number} not found")

        # Save current content as a new version before restoring
        current_content = doc.get("content", "")
        if current_content:
            current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            await svc.db.create_document_version(
                document_id=document_id,
                content=current_content,
                content_hash=current_hash,
                change_type="updated",
                title=doc.get("title"),
                metadata=doc.get("metadata"),
                change_reason=f"Before restore to version {version_number}",
                changed_by=user.user_id,
                confluence_version=doc.get("confluence_version"),
            )

        # Update document with restored content
        restored_content = version_to_restore.get("content", "")
        await svc.db.update_document_fields(
            document_id,
            {
                "content": restored_content,
                "status": "uploaded",
                "progress": 0,
            }
        )

        # Create a new version marking the restore
        restored_hash = hashlib.sha256(restored_content.encode("utf-8")).hexdigest()
        new_version = await svc.db.create_document_version(
            document_id=document_id,
            content=restored_content,
            content_hash=restored_hash,
            change_type="restored",
            title=version_to_restore.get("title") or doc.get("title"),
            metadata=version_to_restore.get("metadata"),
            change_reason=payload.reason or f"Restored from version {version_number}",
            changed_by=user.user_id,
            confluence_version=version_to_restore.get("confluence_version"),
        )

        # Re-index the document
        await worker.enqueue(dataset_id, document_id)

        return {
            "status": "success",
            "document_id": document_id,
            "restored_from_version": version_number,
            "new_version": new_version.get("version_number") if new_version else None,
            "message": f"Document restored to version {version_number}. Re-indexing started.",
        }

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValidationFailedError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/knowledge/worker/status")
async def get_worker_status(
    worker: KnowledgeWorker = Depends(get_knowledge_worker),
):
    """Diagnostic endpoint to check worker status."""
    return {
        "running": worker._running,
        "queue_size": worker.queue.qsize(),
        "worker_count": len(worker._workers),
        "workers_alive": [not t.done() for t in worker._workers],
    }


@router.post("/knowledge/{dataset_id}/documents/{document_id}/force-complete")
async def force_complete_document(
    dataset_id: str,
    document_id: str,
    svc: KnowledgeService = Depends(get_knowledge_service),
):
    """Force complete a stuck document (admin/debug only)."""
    try:
        await svc.db.update_document_status(document_id, status="completed", progress=100)
        return {"status": "completed", "document_id": document_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
