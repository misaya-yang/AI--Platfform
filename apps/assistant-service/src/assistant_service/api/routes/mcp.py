"""Internal Gateway-signed MCP discovery route."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from ai_gateway_core.persistence.repositories.mcp_repository import (
    MCPAuthorizationError,
    MCPNotFoundError,
    MCPRepositoryError,
    MCPValidationError,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from ...auth import UserContext, get_user_context
from ...core.mcp.client import MCPError
from ...core.mcp.runtime import MCPSecretUnavailable

router = APIRouter(prefix="/mcp")


class _DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    principal_type: Literal["service_account", "user_delegated"]


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", "")
        or getattr(request.state, "trace_id", "")
        or "mcp-request"
    )


def _error(request: Request, status: int, code: str, message: str) -> None:
    raise HTTPException(
        status_code=status,
        detail={"code": code, "message": message, "request_id": _request_id(request)},
    )


@router.post("/servers/{server_id}/discover")
async def discover_server(
    server_id: UUID,
    payload: _DiscoveryRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
):
    service = getattr(request.app.state, "mcp_discovery_service", None)
    repository = getattr(request.app.state, "mcp_repository", None)
    if service is None or repository is None:
        _error(request, 503, "MCP_RUNTIME_UNAVAILABLE", "MCP runtime unavailable")
    try:
        result = await service.discover(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            server_id=server_id,
            connection_id=payload.connection_id,
            principal_type=payload.principal_type,
            repository=repository,
        )
    except MCPNotFoundError:
        _error(request, 404, "MCP_SERVER_NOT_FOUND", "MCP server not found")
    except MCPAuthorizationError as exc:
        _error(request, 403, exc.code, "MCP discovery is not authorized")
    except MCPValidationError as exc:
        _error(request, 422, exc.code, "MCP discovery result is invalid")
    except MCPSecretUnavailable:
        _error(request, 503, "MCP_SECRET_UNAVAILABLE", "MCP credential unavailable")
    except MCPError as exc:
        _error(request, 502, exc.stable_code, "MCP server unavailable")
    except MCPRepositoryError:
        _error(request, 503, "MCP_STORAGE_UNAVAILABLE", "MCP storage unavailable")
    request_id = _request_id(request)
    return {
        **result,
        "request_id": request_id,
        "audit_ref": f"request:{request_id}",
    }


__all__ = ["router"]
