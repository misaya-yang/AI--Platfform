"""Capability catalog fetch and validation for Runtime threads.

ARC-02 split of ``control_plane.py``.  This module owns the control-plane
side of the capability preflight: fetching the tenant-scoped read-only
catalog, applying the worker write gate, and pinning the exact live
descriptor allowlist into the turn's readonly payload.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from .snapshot_builder import (
    allowlisted_catalog_descriptors,
    validate_catalog_descriptor,
)
from .types import DISCOVERY_BRIDGE_NAMES, AgentRuntimeControlError

if TYPE_CHECKING:
    from ..control_plane import AgentRuntimeControlPlane


def worker_ready_for_writes() -> bool:
    return (
        os.getenv("AI_PLATFORM_CAPABILITY_WORKER_ENABLED", "").lower() == "true"
        and os.getenv("AI_PLATFORM_CAPABILITY_WORKER_WRITES_ENABLED", "").lower() == "true"
        and bool(os.getenv("AI_PLATFORM_CAPABILITY_WORKER_URL", "").strip())
        and bool(os.getenv("AI_PLATFORM_CAPABILITY_LEASE_SIGNING_SECRET", "").strip())
    )


async def fetch_capability_catalog(
    plane: AgentRuntimeControlPlane,
    readonly: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    model_id: str,
    capability_revision: int,
    capability_allowlist: list[dict[str, Any]] | None = None,
) -> None:
    """Fetch stable read-only schemas before the first Thread is created."""

    if not plane.capability_plane_url:
        return
    response = await plane.http_client.post(
        f"{plane.capability_plane_url}/catalog",
        headers={
            "x-ai-platform-internal-token": plane.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        },
        json={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "model_id": model_id,
            "capability_revision": capability_revision,
            **(
                {"capability_allowlist": capability_allowlist}
                if capability_allowlist is not None
                else {}
            ),
        },
    )
    if response.status_code >= 400:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_UNAVAILABLE", status_code=503
        )
    payload = response.json()
    tools = payload.get("tools") if isinstance(payload, dict) else None
    mcp = payload.get("mcp", []) if isinstance(payload, dict) else None
    deferred = payload.get("deferred", []) if isinstance(payload, dict) else None
    if (
        not isinstance(tools, list)
        or not isinstance(mcp, list)
        or not isinstance(deferred, list)
    ):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_INVALID", status_code=503
        )
    attachment_tools = readonly.get("attachment_tools") or []
    if not isinstance(attachment_tools, list):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_INVALID", status_code=503
        )
    if deferred and not worker_ready_for_writes():
        if capability_allowlist is not None:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_WRITE_CAPABILITY_NOT_MIGRATED",
                status_code=409,
            )
        deferred = []
    readonly["tools"] = [
        validate_catalog_descriptor(
            descriptor,
            tenant_id=tenant_id,
            capability_revision=capability_revision,
        )
        for descriptor in tools
    ]
    readonly["mcp"] = [
        validate_catalog_descriptor(
            descriptor,
            tenant_id=tenant_id,
            capability_revision=capability_revision,
        )
        for descriptor in mcp
    ]
    deferred = [
        validate_catalog_descriptor(
            descriptor,
            tenant_id=tenant_id,
            capability_revision=capability_revision,
            allow_deferred=True,
        )
        for descriptor in deferred
    ]
    requested_tool_names = readonly.get("responses_tool_names")
    if requested_tool_names is not None:
        catalog_by_name: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for kind, descriptors in (
            ("tools", tools),
            ("mcp", mcp),
            ("deferred", deferred),
        ):
            for descriptor in descriptors:
                catalog_by_name.setdefault(str(descriptor["name"]), []).append(
                    (kind, descriptor)
                )
        selected_tools: list[dict[str, Any]] = []
        selected_mcp: list[dict[str, Any]] = []
        for name in requested_tool_names:
            matches = catalog_by_name.get(name, [])
            if len(matches) != 1:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_RESPONSE_TOOL_NOT_FOUND", status_code=400
                )
            kind, descriptor = matches[0]
            if descriptor.get("read_only") is not True:
                # Public Responses cannot mint a write-capable descriptor;
                # those require a signed AgentSpec and approval contract.
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_RESPONSE_TOOL_NOT_READONLY", status_code=409
                )
            if kind == "tools":
                selected_tools.append(descriptor)
            else:
                selected_mcp.append(descriptor)
        tools = selected_tools
        mcp = selected_mcp
        deferred = []
        bridges: list[dict[str, Any]] = []
    else:
        bridges = [item for item in readonly["tools"] if item.get("name") in DISCOVERY_BRIDGE_NAMES]
    bound_tools = [
        item for item in tools if item.get("name") not in DISCOVERY_BRIDGE_NAMES
    ]
    allowed_tools = allowlisted_catalog_descriptors(bound_tools, capability_allowlist)
    allowed_mcp = allowlisted_catalog_descriptors(mcp, capability_allowlist)
    allowed_deferred = (
        allowlisted_catalog_descriptors(deferred, capability_allowlist)
        if capability_allowlist is not None
        else deferred
    )
    readonly["tools"] = allowed_tools + bridges
    readonly["mcp"] = allowed_mcp
    readonly["deferred"] = allowed_deferred
    if capability_allowlist is not None and (
        len(allowed_tools) + len(allowed_mcp) + len(allowed_deferred)
        != len(capability_allowlist)
    ):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_SCOPE_MISMATCH",
            status_code=409,
        )
    # Every turn, including the generic Assistant without an AgentSpec,
    # carries an exact live descriptor allowlist. Preserve any signed
    # connector binding while replacing only version/schema with the
    # values from the live catalog (a connector may publish without a
    # schema hash and receive it here).
    live_descriptors = [
        *allowed_tools,
        *allowed_mcp,
        *allowed_deferred,
        *bridges,
        *attachment_tools,
    ]
    final_allowlist: list[dict[str, Any]] = []
    for item in live_descriptors:
        matches = [
            entry
            for entry in (capability_allowlist or [])
            if str(entry.get("id") or "") == str(item.get("id") or "")
        ]
        if len(matches) > 1:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_SCOPE_MISMATCH",
                status_code=409,
            )
        entry = dict(matches[0]) if matches else {}
        entry.update(
            {
                # Capability descriptors bind to their runtime kind.  A
                # read_attachment descriptor is an internal tool even
                # though it is implicit in the signed attachment refs.
                "type": str(item["kind"]),
                "name": str(item["name"]),
                "id": str(item["id"]),
                "version": item.get("version"),
                "schema_hash": item.get("schema_hash"),
            }
        )
        final_allowlist.append(entry)
    readonly["capability_allowlist"] = final_allowlist
    readonly["attachment_tools"] = attachment_tools


__all__ = ["fetch_capability_catalog", "worker_ready_for_writes"]
