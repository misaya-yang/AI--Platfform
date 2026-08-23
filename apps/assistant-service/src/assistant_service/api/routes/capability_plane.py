"""Private, read-only capability plane for the Agent Runtime.

The endpoint deliberately has no model access and no Agent loop. It reuses
the canonical RegistryToolInvoker so tenant policy, argument validation, audit,
and Knowledge/file/web implementations remain in one place.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...auth.user_context import UserContext
from ...core.tool_invocation_contracts import CapabilityAllowlist, ToolInvocationContext
from ...core.tools.tool_registry import tool_operation_kind
from ..deps import get_assistant_service

router = APIRouter(prefix="/internal/v1/capabilities", tags=["Internal Capabilities"])

_MAX_RESULT_CHARS = 64 * 1024
_MAX_METADATA_KEYS = frozenset(
    {
        "execution_id",
        "status",
        "exit_code",
        "duration_ms",
        "output_files_count",
        "side_effect_state",
        "approval_required",
        "approval_id",
        "queue_state",
        "command_id",
    }
)


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
    capability_allowlist: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    expected_tool: dict[str, Any]


class CapabilityCatalogRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    capability_revision: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=255)
    capability_allowlist: list[dict[str, Any]] | None = Field(default=None, max_length=256)


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
        and tool_operation_kind(definition) == "read"
        and metadata.get("read_only") is True
        and not bool(getattr(definition, "requires_confirmation", False))
    )


def _bounded_text(value: Any, *, limit: int = _MAX_RESULT_CHARS) -> str:
    """Return a bounded, non-secret projection for the internal wire contract."""

    text = str(value or "")
    if len(text) <= limit:
        return text
    suffix = "...[truncated]"
    return f"{text[: limit - len(suffix)]}{suffix}"


def _bounded_result(value: Any) -> Any:
    """Keep structured results structured while enforcing the response bound."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return _bounded_text(value)
    if len(encoded) <= _MAX_RESULT_CHARS:
        return value
    return {
        "truncated": True,
        "preview": _bounded_text(encoded, limit=_MAX_RESULT_CHARS),
    }


def _public_metadata(value: Any) -> dict[str, Any]:
    """Project only bounded scalar execution metadata to the Runtime."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(_MAX_METADATA_KEYS):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = _bounded_text(item, limit=256) if isinstance(item, str) else item
    return result


def _bound_dataset_arguments(
    arguments: dict[str, Any],
    bound_dataset_ids: list[str],
    definition: Any | None = None,
) -> dict[str, Any]:
    """Bind datasets only for tools whose schema declares that parameter.

    The capability lease is still attached to the invocation context for every
    tool.  Only knowledge-style tools opt into argument injection; adding a
    ``dataset_ids`` key to a discovery bridge or a Skill would otherwise make
    its strict schema reject an otherwise valid call.
    """

    normalized = dict(arguments)
    schema = {}
    if definition is not None:
        schema_getter = getattr(definition, "json_argument_schema", None)
        if callable(schema_getter):
            candidate = schema_getter()
            if isinstance(candidate, dict):
                schema = candidate
        if not schema:
            schema_getter = getattr(definition, "model_argument_schema", None)
            if callable(schema_getter):
                candidate = schema_getter()
                if isinstance(candidate, dict):
                    schema = candidate
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or "dataset_ids" not in properties:
        return normalized

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
    request: Request,
    payload: CapabilityInvokeRequest | CapabilityCatalogRequest,
    *,
    readonly_only: bool = True,
) -> list[Any]:
    _assert_forwarded_identity(request, payload)
    assistant = get_assistant_service(request)
    invoker = getattr(assistant, "tool_invoker", None)
    getter = getattr(invoker, "get_tool_definitions_filtered", None)
    if not callable(getter):
        raise HTTPException(status_code=503, detail="capability catalog unavailable")
    snapshot_allowlist: list[dict[str, Any]] | None = None
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
                   AND run.engine = 'agent_runtime'
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
        readonly_snapshot = snapshot.get("readonly_capabilities", {})
        snapshot_has_allowlist = (
            isinstance(readonly_snapshot, dict)
            and "capability_allowlist" in readonly_snapshot
        )
        snapshot_allowlist = (
            readonly_snapshot.get("capability_allowlist")
            if isinstance(readonly_snapshot, dict)
            else None
        )
        if snapshot_has_allowlist and not isinstance(snapshot_allowlist, list):
            raise HTTPException(status_code=403, detail="runtime capability scope mismatch")
        if payload.capability_allowlist != snapshot_allowlist:
            raise HTTPException(status_code=403, detail="runtime capability scope mismatch")
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
    active_allowlist = (
        snapshot_allowlist
        if isinstance(payload, CapabilityInvokeRequest)
        else payload.capability_allowlist
    )
    if active_allowlist is not None:
        from ...core.tools.tool_discovery import DISCOVERY_TOOL_NAMES

        allowed_names = {
            str(item.get("name") or "")
            for item in active_allowlist
            if isinstance(item, dict)
        }
        allowed_names.update(DISCOVERY_TOOL_NAMES)
        definitions = [item for item in definitions if item.name in allowed_names]
    if readonly_only:
        return [definition for definition in definitions if _is_readonly(definition)]
    return list(definitions)


def _descriptor(
    definition: Any, payload: CapabilityCatalogRequest | CapabilityInvokeRequest
) -> dict[str, Any]:
    """Build the stable level-0 descriptor shared by all capability kinds."""

    metadata = dict(getattr(definition, "capability_metadata", None) or {})
    kind = str(metadata.get("kind") or "tool")
    protocol = str(metadata.get("protocol") or ("mcp" if kind == "mcp" else "internal"))
    operation_kind = tool_operation_kind(definition, binding_type="mcp" if kind == "mcp" else "")
    read_only = _is_readonly(definition)
    raw_tags = metadata.get("tags") or metadata.get("trigger_examples") or []
    tags = [str(tag)[:80] for tag in raw_tags if str(tag).strip()][:16]
    schema = definition.model_argument_schema()
    canonical_schema = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    schema_hash = "sha256:" + hashlib.sha256(canonical_schema.encode("utf-8")).hexdigest()
    if kind == "skill":
        resource_id = str(metadata.get("skill_name") or definition.name)
        resource_version = metadata.get("version_id")
        content_hash = str(metadata.get("content_hash") or "")
        if re.fullmatch(r"[0-9a-f]{64}", content_hash):
            schema_hash = f"sha256:{content_hash}"
    else:
        resource_id = str(metadata.get("id") or metadata.get("resource_id") or definition.name)
        resource_version = metadata.get("version") or metadata.get("resource_version")
    return {
        "name": str(definition.name),
        "id": resource_id,
        "version": str(resource_version) if resource_version is not None else None,
        "schema_hash": schema_hash,
        "description": _bounded_text(definition.description, limit=1200),
        # Schemas remain exact and locally validated; the registry rejects
        # external refs and oversized schemas before they reach this boundary.
        "schema": schema,
        "source": str(metadata.get("source") or metadata.get("capability_source") or "assistant"),
        "kind": kind,
        "protocol": protocol,
        "read_only": read_only,
        "risk": str(getattr(getattr(definition, "risk_level", None), "value", "unknown")),
        "operation_kind": operation_kind,
        "requires_confirmation": bool(getattr(definition, "requires_confirmation", False)),
        "approval_required": not read_only,
        "tenant_id": payload.tenant_id,
        "capability_revision": payload.capability_revision,
        "tags": tags,
        "category": str(getattr(getattr(definition, "category", None), "value", "utility")),
    }


def _intersect_allowlisted_descriptors(
    descriptors: list[dict[str, Any]],
    allowlist: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return only exact live-definition matches for a signed capability set."""

    if allowlist is None:
        return descriptors
    selected: list[dict[str, Any]] = []
    for expected in allowlist:
        matches = [
            descriptor
            for descriptor in descriptors
            if _descriptor_matches_allowlist(descriptor, expected)
        ]
        if len(matches) != 1:
            raise HTTPException(
                status_code=409,
                detail="capability allowlist does not match live definition",
            )
        selected.append(matches[0])
    return selected


def _descriptor_matches_allowlist(
    descriptor: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    if str(expected.get("id") or "") != descriptor["id"]:
        return False
    if expected.get("version") != descriptor["version"]:
        return False
    if expected.get("schema_hash") == descriptor["schema_hash"]:
        return True
    return expected.get("type") == "platform" and expected.get("schema_hash") is None


@router.post("/catalog")
async def capability_catalog(
    request: Request,
    payload: CapabilityCatalogRequest,
) -> dict[str, Any]:
    _authorize(request)
    definitions = await _authorized_tools(request, payload, readonly_only=False)
    descriptors = [
        _descriptor(definition, payload)
        for definition in sorted(definitions, key=lambda item: item.name.casefold())
    ]
    if payload.capability_allowlist is not None:
        from ...core.tools.tool_discovery import DISCOVERY_TOOL_NAMES

        bridges = [item for item in descriptors if item["name"] in DISCOVERY_TOOL_NAMES]
        bound = [item for item in descriptors if item["name"] not in DISCOVERY_TOOL_NAMES]
        descriptors = _intersect_allowlisted_descriptors(bound, payload.capability_allowlist)
        descriptors.extend(bridges)
    readonly = [item for item in descriptors if item["read_only"]]
    deferred = [item for item in descriptors if not item["read_only"]]
    return {
        # Keep the V1 envelope while extending descriptors additively; the
        # Runtime's read-only projector validates this version explicitly.
        "schema_version": "agent-readonly-capability/v1",
        "tenant_id": payload.tenant_id,
        "capability_revision": payload.capability_revision,
        # Existing Runtime consumers intentionally receive only read-only
        # dynamic tools.  Write/unknown descriptors are metadata-only until
        # an approval turn is created through the execution gateway.
        "tools": [item for item in readonly if item["kind"] != "mcp"],
        "mcp": [item for item in readonly if item["kind"] == "mcp"],
        "deferred": deferred,
    }


@router.post("/invoke")
async def capability_invoke(
    request: Request,
    payload: CapabilityInvokeRequest,
) -> dict[str, Any]:
    _authorize(request)
    definitions = await _authorized_tools(request, payload, readonly_only=False)
    definition = next((item for item in definitions if item.name == payload.tool), None)
    if definition is None:
        raise HTTPException(status_code=403, detail="capability is not authorized")
    live_descriptor = _descriptor(definition, payload)
    live_identity = {
        "type": live_descriptor["kind"],
        "name": live_descriptor["name"],
        "id": live_descriptor["id"],
        "version": live_descriptor["version"],
        "schema_hash": live_descriptor["schema_hash"],
    }
    if payload.expected_tool != live_identity or sum(
        item == live_identity for item in (payload.capability_allowlist or [])
    ) != 1:
        raise HTTPException(status_code=409, detail="capability binding changed")
    assistant = get_assistant_service(request)
    invoker = assistant.tool_invoker
    operation_kind = tool_operation_kind(definition)
    read_only = _is_readonly(definition)
    from ...core.tools.tool_discovery import is_tool_discovery_bridge

    # The bridges are the only capability that may inspect the complete
    # already-authorized catalog.  Direct calls remain one-tool allowlists.
    allowlisted_names = {payload.tool}
    if is_tool_discovery_bridge(payload.tool):
        allowlisted_names.update(item.name for item in definitions)
    context = ToolInvocationContext(
        session_id=payload.session_id,
        user_id=payload.user_id,
        tenant_id=payload.tenant_id,
        request_id=str(uuid.uuid4()),
        run_id=payload.run_id,
        user=_identity(payload),
        capability_allowlist=CapabilityAllowlist(tool_names=frozenset(allowlisted_names)),
        kb_dataset_ids=list(payload.bound_dataset_ids),
        metadata={
            "capability_plane": True,
            "capability_revision": payload.capability_revision,
            # The capability plane never dispatches a write/unknown operation
            # directly.  The execution gateway turns this marker into a
            # scoped approval record and returns before handler dispatch.
            **({"_middleware_approval_required": True} if not read_only else {}),
        },
    )
    arguments = _bound_dataset_arguments(
        payload.arguments,
        payload.bound_dataset_ids,
        definition,
    )
    if not read_only:
        gateway = getattr(assistant, "execution_gateway", None)
        invoke_gateway = getattr(gateway, "invoke_tool", None)
        if not callable(invoke_gateway):
            raise HTTPException(status_code=503, detail="capability approval gateway unavailable")
        # This is a private control argument consumed by the gateway before
        # handler dispatch.  Force it here instead of trusting caller input or
        # context metadata; the gateway's approval state machine owns the
        # actual approval record and result projection.
        result = await invoke_gateway(
            payload.tool,
            {**arguments, "_middleware_approval_required": True},
            context,
        )
    else:
        result = await invoker.invoke(payload.tool, arguments, context)
    public_result = _bounded_result(result.result)
    output = public_result if result.success else _bounded_text(result.error or "capability failed")
    return {
        "schema_version": "agent-readonly-capability-result/v1",
        "tool": payload.tool,
        "tool_call_id": result.call_id,
        "success": result.success,
        "read_only": read_only,
        "operation_kind": operation_kind,
        "approval_required": bool((result.metadata or {}).get("approval_required")) or not read_only,
        "content_items": [{"type": "input_text", "text": _bounded_text(output)}],
        "output": public_result,
        "error": _bounded_text(result.error) if result.error else None,
        "metadata": _public_metadata(result.metadata),
    }
