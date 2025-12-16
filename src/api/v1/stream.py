from __future__ import annotations

import logging
import traceback

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..deps import AuthContext, get_auth_context, get_dispatcher
from ..schemas.request import UnifiedRequestSchema
from ..schemas.response import StreamChunkSchema
from ...core.exceptions import GatewayError
from ...core.gateway.dispatcher import GatewayDispatcher


router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/stream")
async def stream(
    body: UnifiedRequestSchema,
    request: Request,
    dispatcher: GatewayDispatcher = Depends(get_dispatcher),
    auth: AuthContext = Depends(get_auth_context),
):
    domain_req = body.to_domain(
        default_user_id=auth.user_id, default_tenant_id=auth.tenant_id
    )
    client_ip = request.client.host if request.client else None

    async def event_generator():
        chunk_count = 0
        try:
            async for chunk in dispatcher.stream(
                domain_req, roles=auth.roles, client_ip=client_ip
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
        },
    )
