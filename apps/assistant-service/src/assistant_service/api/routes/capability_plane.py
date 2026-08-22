"""Private, read-only capability plane for the Codex Runtime.

The endpoint deliberately has no model access and no Agent loop. It reuses
the canonical RegistryToolInvoker so tenant policy, argument validation, audit,
and Knowledge/file/web implementations remain in one place.
"""

from __future__ import annotations

import hmac
import os
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...auth.user_context import UserContext
from ...core.tool_invocation_contracts import CapabilityAllowlist, ToolInvocationContext
from ..deps import get_assistant_service

router = APIRouter(prefix="/internal/v1/capabilities", tags=["Internal Capabilities"])


class CapabilityInvokeRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    capability_revision: int = Field(ge=1)
    snapshot_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:/-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    bound_dataset_ids: list[str] = Field(default_factory=list, max_length=8)


class CapabilityCatalogRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    capability_revision: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=255)


def _authorize(request: Request) -> None:
    expected = os.getenv("AI_PLATFORM_INTERNAL_TOKEN", "")
    provided = request.headers.get("x-ai-platform-internal-token", "")
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="capability plane authentication failed")


def _assert_forwarded_identity(
    request: Request, payload: CapabilityInvokeRequest | CapabilityCatalogRequest
) -> None:
    """Bind the structured payload to the Gateway-forwarded identity.

    The internal token authenticates the hop, but is intentionally not an
    identity assertion.  Runtime always sends these headers from its immutable
    thread/lease scope; accepting a body that disagrees would let a token
    holder accidentally (or deliberately) enumerate another tenant.
    """

    expected = {
        "x-ai-tenant-id": payload.tenant_id,
        "x-ai-user-id": payload.user_id,
        "x-ai-session-id": payload.session_id,
    }
    for header, value in expected.items():
        if not hmac.compare_digest(request.headers.get(header, ""), value):
            raise HTTPException(status_code=403, detail="capability identity mismatch")


def _identity(payload: CapabilityInvokeRequest | CapabilityCatalogRequest) -> UserContext:
    return UserContext(
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
        user_tier="normal",
        user_type="runtime",
        roles=["user"],
    )


def _is_readonly(definition: Any) -> bool:
    metadata = getattr(definition, "capability_metadata", None) or {}
    return (
        getattr(getattr(definition, "risk_level", None), "value", None) == "low"
        and metadata.get("operation_kind") == "read"
        and metadata.get("read_only") is True
        and not bool(getattr(definition, "requires_confirmation", False))
    )


def _bound_dataset_arguments(
    arguments: dict[str, Any], bound_dataset_ids: list[str]
) -> dict[str, Any]:
    """Allow a tool call to narrow, never widen, the lease-bound datasets."""

    normalized = dict(arguments)
    requested = normalized.get("dataset_ids")
    if requested is None:
        normalized["dataset_ids"] = list(bound_dataset_ids)
    elif (
        not isinstance(requested, list)
        or not all(isinstance(item, str) for item in requested)
        or not set(requested).issubset(set(bound_dataset_ids))
    ):
        raise HTTPException(status_code=403, detail="dataset scope exceeds runtime lease")
    return normalized


async def _authorized_tools(
    request: Request, payload: CapabilityInvokeRequest | CapabilityCatalogRequest
) -> list[Any]:
    _assert_forwarded_identity(request, payload)
    assistant = get_assistant_service(request)
    invoker = getattr(assistant, "tool_invoker", None)
    getter = getattr(invoker, "get_tool_definitions_filtered", None)
    if not callable(getter):
        raise HTTPException(status_code=503, detail="capability catalog unavailable")
    if isinstance(payload, CapabilityCatalogRequest):
        model_registry = getattr(assistant, "model_registry", None)
        model = model_registry.get_model(payload.model_id) if model_registry is not None else None
        if model is None or int(getattr(model, "capability_revision", 1)) != payload.capability_revision:
            raise HTTPException(status_code=409, detail="model capability revision mismatch")
    if isinstance(payload, CapabilityInvokeRequest):
        database = getattr(request.app.state, "database", None)
        if database is None:
            raise HTTPException(status_code=503, detail="capability lease store unavailable")
        lease = await database.fetchrow(
            """
            SELECT snapshot.snapshot AS runtime_snapshot,
                   EXISTS (
                SELECT 1
                  FROM assistant_runtime_model_leases AS lease
                  JOIN assistant_runtime_snapshots AS snapshot
                    ON snapshot.snapshot_id = lease.snapshot_id
                   AND snapshot.run_id = lease.run_id
                   AND snapshot.tenant_id = lease.tenant_id
                   AND snapshot.user_id = lease.user_id
                   AND snapshot.session_id = lease.session_id
                   AND snapshot.capability_revision = lease.capability_revision
                 JOIN assistant_runs AS run ON run.run_id = lease.run_id
                 WHERE lease.snapshot_id = $1
                   AND lease.run_id = $2
                   AND lease.tenant_id = $3
                   AND lease.user_id = $4
                   AND lease.session_id = $5
                   AND lease.capability_revision = $6
                   AND lease.status = 'active'
                   AND lease.expires_at > NOW()
                   AND run.status = 'running'
                   AND run.engine = 'codex_harness'
                   AND NOT EXISTS (
                       SELECT 1 FROM assistant_runtime_snapshot_revocations AS revoked
                        WHERE revoked.snapshot_id = lease.snapshot_id
                   )
                   ) AS valid
              FROM assistant_runtime_snapshots AS snapshot
             WHERE snapshot.snapshot_id = $1
               AND snapshot.run_id = $2
               AND snapshot.tenant_id = $3
               AND snapshot.user_id = $4
               AND snapshot.session_id = $5
               AND snapshot.capability_revision = $6
            """,
            payload.snapshot_id,
            payload.run_id,
            payload.tenant_id,
            payload.user_id,
            payload.session_id,
            payload.capability_revision,
        )
        if not lease or not bool(lease.get("valid")):
            raise HTTPException(status_code=404, detail="runtime capability lease not found")
        snapshot = lease.get("runtime_snapshot") or {}
        snapshot_items = snapshot.get("readonly_capabilities", {}).get("items", [])
        bound_from_snapshot = {
            item.get("payload", {}).get("dataset_id")
            for item in snapshot_items
            if item.get("kind") == "knowledge"
            and item.get("tenant_id") == payload.tenant_id
            and item.get("capability_revision") == payload.capability_revision
        }
        if set(payload.bound_dataset_ids) != bound_from_snapshot:
            raise HTTPException(status_code=403, detail="runtime dataset scope mismatch")
    user = _identity(payload)
    context = ToolInvocationContext(
        session_id=payload.session_id,
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
        request_id=str(uuid.uuid4()),
        run_id=getattr(payload, "run_id", "catalog"),
        user=user,
        metadata={
            "capability_plane": True,
            "capability_revision": payload.capability_revision,
        },
    )
    definitions = await getter(context)
    return [definition for definition in definitions if _is_readonly(definition)]


@router.post("/catalog")
async def capability_catalog(request: Request, payload: CapabilityCatalogRequest):
    _authorize(request)
    definitions = await _authorized_tools(request, payload)
    descriptors = [
        {
            "name": definition.name,
            "description": definition.description,
            "schema": definition.model_argument_schema(),
            "source": (definition.capability_metadata or {}).get("source", "assistant"),
            "kind": (definition.capability_metadata or {}).get("kind", "tool"),
            "protocol": (definition.capability_metadata or {}).get("protocol", "internal"),
            "read_only": True,
            "tenant_id": payload.tenant_id,
            "capability_revision": payload.capability_revision,
            "tags": list((definition.capability_metadata or {}).get("tags") or []),
        }
        for definition in sorted(definitions, key=lambda item: item.name.casefold())
    ]
    return {
        "schema_version": "codex-readonly-capability/v1",
        "tenant_id": payload.tenant_id,
        "capability_revision": payload.capability_revision,
        "tools": [item for item in descriptors if item["kind"] != "mcp"],
        "mcp": [item for item in descriptors if item["kind"] == "mcp"],
    }


@router.post("/invoke")
async def capability_invoke(request: Request, payload: CapabilityInvokeRequest):
    _authorize(request)
    definitions = await _authorized_tools(request, payload)
    definition = next((item for item in definitions if item.name == payload.tool), None)
    if definition is None:
        raise HTTPException(status_code=403, detail="read-only capability is not authorized")
    assistant = get_assistant_service(request)
    invoker = assistant.tool_invoker
    context = ToolInvocationContext(
        session_id=payload.session_id,
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
        request_id=str(uuid.uuid4()),
        run_id=payload.run_id,
        user=_identity(payload),
        capability_allowlist=CapabilityAllowlist(tool_names=frozenset({payload.tool})),
        kb_dataset_ids=list(payload.bound_dataset_ids),
        metadata={
            "capability_plane": True,
            "capability_revision": payload.capability_revision,
        },
    )
    arguments = _bound_dataset_arguments(payload.arguments, payload.bound_dataset_ids)
    result = await invoker.invoke(payload.tool, arguments, context)
    output = result.result if result.success else result.error or "read-only capability failed"
    return {
        "schema_version": "codex-readonly-capability-result/v1",
        "tool": payload.tool,
        "tool_call_id": result.call_id,
        "success": result.success,
        "content_items": [{"type": "input_text", "text": str(output or "")}],
        "output": result.result,
        "error": result.error,
        "metadata": result.metadata or {},
    }
