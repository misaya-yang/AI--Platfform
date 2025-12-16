from __future__ import annotations

import logging
import traceback
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..deps import get_dispatcher, get_user_context
from ..schemas.request import UnifiedRequestSchema
from ..schemas.response import StreamChunkSchema
from ...core.exceptions import GatewayError
from ...core.gateway.dispatcher import GatewayDispatcher
from ...core.auth.user_resolver import UserContext


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/stream")
async def stream(
    body: UnifiedRequestSchema,
    request: Request,
    dispatcher: GatewayDispatcher = Depends(get_dispatcher),
    user: UserContext = Depends(get_user_context),
):
    domain_req = body.to_domain(
        default_user_id=user.user_id, default_tenant_id=user.tenant_id
    )
    client_ip = request.client.host if request.client else None

    # For streaming, create/resolve the session up-front so clients can read it
    # from response headers (async generators don't run until the response is sent).
    session_id_header: Optional[str] = None
    try:
        registry = getattr(request.app.state, "registry", None)
        service = await registry.get(domain_req.service_id) if registry else None
        if service and getattr(service, "session_enabled", False):
            session = await dispatcher.session_manager.get_or_create(
                domain_req.session_id,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                service_id=service.service_id,
            )
            domain_req.session_id = session.session_id
            session_id_header = session.session_id
    except GatewayError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    async def event_generator():
        chunk_count = 0
        try:
            async for chunk in dispatcher.stream(
                domain_req, roles=user.roles, client_ip=client_ip
            ):
                payload = StreamChunkSchema.from_domain(chunk).model_dump_json()
                yield f"data: {payload}\n\n"
                chunk_count += 1
            
            logger.debug(f"Stream completed, sent {chunk_count} chunks")
        except GatewayError as exc:
            logger.error(f"Stream gateway error: {exc}")
            yield f"event: error\ndata: {str(exc)}\n\n"
        except Exception as exc:
            logger.error(f"Stream unexpected error: {exc}\n{traceback.format_exc()}")
            yield f"event: error\ndata: {str(exc)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
            **({"X-Session-Id": session_id_header} if session_id_header else {}),
        },
    )
