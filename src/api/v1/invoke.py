from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import AuthContext, get_auth_context, get_dispatcher
from ..schemas.request import UnifiedRequestSchema
from ..schemas.response import UnifiedResponseSchema
from ...core.exceptions import GatewayError
from ...core.gateway.dispatcher import GatewayDispatcher


router = APIRouter()


@router.post("/invoke", response_model=UnifiedResponseSchema)
async def invoke(
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
        resp = await dispatcher.invoke(domain_req, roles=auth.roles, client_ip=client_ip)
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return UnifiedResponseSchema.from_domain(resp)
