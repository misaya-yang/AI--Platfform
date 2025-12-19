from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile

from ..deps import get_knowledge_service, get_knowledge_worker, get_user_context
from ..schemas.knowledge import (
    BatchDeleteSchema,
    BatchReindexSchema,
    DatasetCreateSchema,
    DatasetPermissionGrantSchema,
    DatasetUpdateSchema,
    DocumentArchiveSchema,
    DocumentBatchCreateSchema,
    DocumentCreateTextSchema,
    DocumentCreateUrlSchema,
    DocumentEnableDisableSchema,
    DocumentUpdateSchema,
    RetrieveRequestSchema,
    SegmentCreateSchema,
    SegmentEnableDisableSchema,
    SegmentUpdateSchema,
)
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
        results, meta = await svc.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=payload.query,
            top_k=payload.top_k,
            mode=payload.mode,
            document_id=payload.document_id,
            alpha=payload.alpha,
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
            alpha=payload.alpha,
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
