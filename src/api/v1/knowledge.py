from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile

from ..deps import get_knowledge_service, get_knowledge_worker, get_user_context
from ..schemas.knowledge import (
    DatasetCreateSchema,
    DatasetPermissionGrantSchema,
    DatasetUpdateSchema,
    DocumentCreateTextSchema,
    RetrieveRequestSchema,
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
