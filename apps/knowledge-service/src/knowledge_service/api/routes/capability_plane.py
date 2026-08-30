"""Private, read-only retrieval entrypoint for the Rust capability worker.

This route is deliberately a thin adapter: authorization and retrieval remain
owned by ``KnowledgeService`` and its existing retrieval pipeline.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from ai_gateway_contracts.capability_proof import CapabilityProofError, verify_capability_proof
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...config import get_settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...services.knowledge.knowledge_service import KnowledgeService
from ..deps import get_knowledge_service

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_CAPABILITY_BODY_BYTES = 256 * 1024


class CapabilityRetrieveRequest(BaseModel):
    """Only the bounded, text-only subset exposed to the Rust worker."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4096)
    top_k: int = Field(default=5, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


def _internal_authorized(request: Request) -> bool:
    expected = get_settings().ai_platform_internal_token
    provided = request.headers.get("x-ai-platform-internal-token", "")
    return bool(expected) and bool(provided) and hmac.compare_digest(expected, provided)


def _proof_authorized(request: Request, *, path: str, body: Any) -> bool:
    secret = get_settings().ai_platform_capability_proof_secret
    proof = request.headers.get("x-ai-capability-proof", "")
    execution_id = request.headers.get("x-ai-execution-id", "")
    run_id = request.headers.get("x-ai-run-id", "")
    tenant_id = request.headers.get("x-ai-tenant-id", "").strip()
    user_id = request.headers.get("x-ai-user-id", "").strip()
    session_id = request.headers.get("x-ai-session-id", "").strip()
    if not secret or not proof or not execution_id or not run_id:
        return False
    try:
        verify_capability_proof(
            secret,
            proof,
            method="POST",
            path=path,
            body=body,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            execution_id=execution_id,
            run_id=run_id,
        )
    except CapabilityProofError:
        return False
    return True


def _runtime_user(request: Request) -> UserContext:
    values = {
        "tenant_id": request.headers.get("x-ai-tenant-id", "").strip(),
        "user_id": request.headers.get("x-ai-user-id", "").strip(),
        "session_id": request.headers.get("x-ai-session-id", "").strip(),
    }
    if not all(values.values()) or any(len(value) > 255 for value in values.values()):
        raise HTTPException(status_code=403, detail="capability identity invalid")
    # Session is a runtime lease binding rather than a UserContext field; the
    # service still receives the immutable tenant/user identity for ACL checks.
    return UserContext(
        user_id=values["user_id"],
        tenant_id=values["tenant_id"],
        user_tier="normal",
        user_type="runtime",
        roles=["user"],
    )


def _text_result(result: Any) -> dict[str, Any]:
    content_type = str(getattr(result, "content_type", "text") or "text").lower()
    if content_type not in {"text", "text/plain"}:
        raise ValidationFailedError("text-only capability result required")
    return {
        "segment_id": result.segment_id,
        "document_id": result.document_id,
        "score": result.score,
        "text": result.text,
        "metadata": result.metadata,
        "content_type": "text",
        "image_url": None,
        "vlm_description": None,
        "associated_images": [],
        "source_type": (result.metadata or {}).get("source_type"),
        "citation_text": (result.metadata or {}).get("citation_text"),
        "source_reference": (result.metadata or {}).get("source_reference", {}),
    }


@router.post("/internal/v2/capabilities/knowledge/{dataset_id}/retrieve")
async def retrieve_capability(
    dataset_id: str,
    request: Request,
    payload: CapabilityRetrieveRequest = Body(...),
    svc: KnowledgeService = Depends(get_knowledge_service),
) -> dict[str, Any]:
    """Retrieve bounded text chunks for an authenticated Rust worker lease."""

    # The Rust worker collapses every non-2xx into
    # ``read_capability_downstream_rejected``, so without a line here a
    # rejected retrieval is invisible on both sides. Log the reason (never the
    # token, proof, or query).
    if not _internal_authorized(request):
        logger.warning(
            "Capability retrieve rejected: internal token mismatch (dataset=%s)",
            dataset_id,
        )
        raise HTTPException(status_code=401, detail="capability authentication failed")
    if not _proof_authorized(
        request,
        path=f"/internal/v2/capabilities/knowledge/{dataset_id}/retrieve",
        body=payload.model_dump(mode="json"),
    ):
        logger.warning(
            "Capability retrieve rejected: proof invalid (dataset=%s)", dataset_id
        )
        raise HTTPException(status_code=401, detail="capability proof invalid")
    if len(await request.body()) > MAX_CAPABILITY_BODY_BYTES:
        raise HTTPException(status_code=413, detail="capability request too large")
    user = _runtime_user(request)
    try:
        # This is the same authoritative tenant/ACL check used by public KB
        # retrieval; do not infer ownership from the URL or worker payload.
        await svc.require_dataset_access(user, dataset_id, required="viewer")
    except (PermissionDeniedError, ValidationFailedError, LookupError, ValueError) as exc:
        # Deliberately collapse forbidden, missing, and deleted datasets in the
        # response; keep the distinction in the log.
        logger.warning(
            "Capability retrieve rejected: dataset access denied (dataset=%s, reason=%s)",
            dataset_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=404, detail="Dataset not found") from None
    try:
        results, metadata = await svc.retrieve(
            user=user,
            dataset_id=dataset_id,
            query=payload.query,
            top_k=payload.top_k,
            score_threshold=payload.threshold,
        )
        return {"results": [_text_result(item) for item in results], "metadata": metadata}
    except ValidationFailedError:
        raise HTTPException(status_code=400, detail="Invalid retrieval request") from None
    except (PermissionDeniedError, KeyError):
        raise HTTPException(status_code=404, detail="Dataset not found") from None
