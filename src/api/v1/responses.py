"""Public ``POST /v1/responses`` boundary.

The gateway owns authentication, tenant/model authorization and rate limiting;
assistant-service owns request validation and the Responses event projection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...core.auth.user_resolver import UserContext
from ..deps import enforce_rate_limit, get_user_context
from ._assistant_proxy import (
    proxy_to_assistant_service,
    reject_client_agent_forgery,
)
from .assistant import _check_model_permission

router = APIRouter(tags=["Responses"])
logger = logging.getLogger(__name__)


def _error(
    *,
    status_code: int,
    code: str,
    message: str,
    param: str | None = None,
    error_type: str = "invalid_request_error",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
        headers=headers,
    )


def _has_authenticated_tenant(user: UserContext) -> bool:
    return bool(
        user.is_authenticated
        and user.user_id
        and user.tenant_id
        and user.user_id != "anonymous"
        and user.tenant_id != "public"
    )


@router.post("/responses")
async def create_response(
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    """Proxy one authenticated Responses request to the canonical Assistant runtime."""

    if not _has_authenticated_tenant(user):
        return _error(
            status_code=401,
            code="authentication_required",
            message="Authentication and tenant identity are required.",
            error_type="authentication_error",
        )
    if request.url.query:
        return _error(
            status_code=400,
            code="unsupported_query_parameters",
            message="Query parameters are not supported for this endpoint.",
        )
    try:
        await enforce_rate_limit(request, user, operation="assistant_chat")
    except HTTPException as exc:
        if exc.status_code != 429:
            raise
        return _error(
            status_code=429,
            code="rate_limit_exceeded",
            message="Rate limit exceeded.",
            error_type="rate_limit_error",
            headers=dict(exc.headers or {}),
        )

    body = await request.body()
    try:
        payload: Any = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(status_code=400, code="invalid_json", message="Invalid JSON body.")
    if not isinstance(payload, dict):
        return _error(
            status_code=400,
            code="invalid_json_object",
            message="Request body must be a JSON object.",
        )
    try:
        reject_client_agent_forgery(request, payload)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return _error(
            status_code=exc.status_code,
            code=str(detail.get("code") or "agent_runtime_field_forbidden").lower(),
            message="Client-supplied Agent runtime fields or headers are forbidden.",
            error_type="invalid_request_error",
        )

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 255:
        return _error(
            status_code=400,
            code="invalid_model",
            message="model must be a non-empty string.",
            param="model",
        )
    model_meta = getattr(request.app.state, "model_meta", None)
    if model_meta is None:
        return _error(
            status_code=503,
            code="model_authorization_unavailable",
            message="Model authorization is temporarily unavailable.",
            param="model",
            error_type="server_error",
        )
    try:
        await _check_model_permission(user, model, model_meta)
    except HTTPException as exc:
        if exc.status_code == 400:
            return _error(
                status_code=400,
                code="model_not_found",
                message="The requested model was not found.",
                param="model",
            )
        if exc.status_code == 403:
            return _error(
                status_code=403,
                code="model_access_denied",
                message="Access to the requested model is denied.",
                param="model",
                error_type="permission_error",
            )
        raise
    except Exception as exc:
        logger.error(
            "Responses model authorization unavailable (exception_type=%s)",
            type(exc).__name__,
        )
        return _error(
            status_code=503,
            code="model_authorization_unavailable",
            message="Model authorization is temporarily unavailable.",
            param="model",
            error_type="server_error",
        )

    return await proxy_to_assistant_service(
        request,
        user,
        path="responses",
        body=body,
    )


__all__ = ["create_response", "router"]
