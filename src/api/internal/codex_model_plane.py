"""Private Responses endpoint consumed only by the Codex Runtime container."""

from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...services.codex_runtime import CodexModelPlaneError

router = APIRouter(
    prefix="/internal/v1/codex-model-plane",
    include_in_schema=False,
)
logger = logging.getLogger(__name__)


def _error(error: CodexModelPlaneError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "type": "runtime_model_plane_error",
                "code": error.code,
                "message": "The private model request was rejected.",
            }
        },
    )


@router.post("/responses")
async def responses(request: Request):
    expected = str(getattr(request.app.state, "codex_model_plane_internal_token", "") or "")
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        return _error(CodexModelPlaneError("RUNTIME_MODEL_PLANE_UNAUTHORIZED", status_code=401))

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(CodexModelPlaneError("RUNTIME_MODEL_REQUEST_INVALID"))
    if not isinstance(body, dict) or body.get("stream") is not True:
        return _error(CodexModelPlaneError("RUNTIME_MODEL_REQUEST_INVALID"))
    metadata_raw = request.headers.get("x-codex-turn-metadata", "")
    try:
        turn_metadata = json.loads(metadata_raw)
    except json.JSONDecodeError:
        return _error(CodexModelPlaneError("RUNTIME_TURN_METADATA_INVALID", status_code=401))
    if not isinstance(turn_metadata, dict):
        return _error(CodexModelPlaneError("RUNTIME_TURN_METADATA_INVALID", status_code=401))
    plane = getattr(request.app.state, "codex_model_plane", None)
    if plane is None:
        return _error(CodexModelPlaneError("RUNTIME_MODEL_PLANE_UNAVAILABLE", status_code=503))
    try:
        authorized = await plane.authorize_and_reserve(
            body=body,
            turn_metadata=turn_metadata,
        )
    except CodexModelPlaneError as error:
        logger.warning(
            "Codex model-plane request rejected code=%s status=%s",
            error.code,
            error.status_code,
        )
        return _error(error)
    return StreamingResponse(
        plane.stream(
            body=body,
            turn_metadata=turn_metadata,
            authorized_call=authorized,
        ),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


__all__ = ["router"]
