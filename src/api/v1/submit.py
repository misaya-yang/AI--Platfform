from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import AuthContext, get_auth_context, get_dispatcher
from ..schemas.request import UnifiedRequestSchema
from ..schemas.response import UnifiedResponseSchema
from ...core.exceptions import GatewayError
from ...core.gateway.dispatcher import GatewayDispatcher
from ...models.response import UnifiedResponse


router = APIRouter()


@router.post("/submit", response_model=UnifiedResponseSchema)
async def submit(
    body: UnifiedRequestSchema,
    request: Request,
    dispatcher: GatewayDispatcher = Depends(get_dispatcher),
    auth: AuthContext = Depends(get_auth_context),
):
    domain_req = body.to_domain(
        default_user_id=auth.user_id, default_tenant_id=auth.tenant_id
    )
    client_ip = request.client.host if request.client else None
    try:
        task_id = await dispatcher.submit(
            domain_req, roles=auth.roles, client_ip=client_ip
        )
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    pending = UnifiedResponse(
        request_id=domain_req.request_id,
        status="pending",
        outputs=[],
        task_id=task_id,
    )
    return UnifiedResponseSchema.from_domain(pending)
