"""Thread and turn lifecycle: ensure/resume/verify, fingerprint CAS, turn issue.

ARC-02 split of ``control_plane.py``.  These functions take the control plane
as their first argument and are bound onto ``AgentRuntimeControlPlane`` by the
facade; ``start_turn`` stays the single orchestrator that resolves the Agent
spec, pins the snapshot, signs the model lease, and starts the Runtime turn.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ai_gateway_core.agents import (
    RUNTIME_MODEL_LEASE_SCHEMA_VERSION,
    RuntimeModelLeaseClaims,
    canonical_runtime_json,
)
from ai_gateway_core.models import resolve_reasoning_option

from ..runtime_configuration import (
    RuntimePlatformConfigError,
    build_runtime_platform_config,
    runtime_platform_config_hash,
)
from .snapshot_builder import (
    attach_read_attachment_descriptors,
    dynamic_tool_fingerprint,
    dynamic_tools,
    readonly_capability_payload,
    runtime_model_config,
    snapshot_capability_allowlist,
    turn_prompt_readonly,
)
from .types import (
    BASE_AGENT_INSTRUCTIONS_V1,
    GENERIC_AGENT_INSTRUCTIONS_V1,
    AgentRuntimeControlError,
    AgentTurn,
    _provider_revision,
)

if TYPE_CHECKING:
    from ..control_plane import AgentRuntimeControlPlane

logger = logging.getLogger(__name__)


async def cleanup_session(
    plane: AgentRuntimeControlPlane,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> bool:
    """Tombstone one Runtime-owned session without mutating its item log."""

    if (
        not session_id
        or len(session_id) > 255
        or any(ord(character) < 32 for character in session_id)
    ):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_SESSION_ID_INVALID", status_code=400
        )
    response = await plane.http_client.post(
        f"{plane.runtime_url}/internal/v1/sessions/{quote(session_id, safe='')}/cleanup",
        headers={
            "x-ai-platform-internal-token": plane.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        },
        json={},
    )
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_SESSION_CLEANUP_FAILED",
            status_code=503 if response.status_code >= 500 else 409,
        )
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("session_id") != session_id:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_SESSION_CLEANUP_INVALID", status_code=503
        )
    return payload.get("status") == "deleted"


async def assignment(plane: AgentRuntimeControlPlane, tenant_id: str, user_id: str, session_id: str):
    resolved = await plane.assignment_store.resolve(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
    )
    if resolved is None or resolved.runtime_owner != "agent_runtime":
        raise AgentRuntimeControlError("AGENT_RUNTIME_ASSIGNMENT_MISMATCH", status_code=403)
    return resolved


async def existing_thread(
    plane: AgentRuntimeControlPlane,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    row = await plane.database.fetchrow(
        """
        SELECT runtime_thread_id, last_sequence, dynamic_tool_fingerprint
          FROM assistant_runtime_threads
         WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
           AND deleted_at IS NULL
        """,
        tenant_id,
        user_id,
        session_id,
    )
    return dict(row) if row else None


def assert_dynamic_tool_fingerprint(
    existing: dict[str, Any], requested_fingerprint: str
) -> None:
    stored_fingerprint = str(existing.get("dynamic_tool_fingerprint") or "")
    if stored_fingerprint != requested_fingerprint:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_THREAD_RECREATE_REQUIRED", status_code=409
        )


async def bind_dynamic_tool_fingerprint(
    plane: AgentRuntimeControlPlane,
    *,
    existing: dict[str, Any],
    fingerprint: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    created_by_this_request: bool = False,
) -> dict[str, Any]:
    """Persist a newly created Thread fingerprint with a null-only CAS.

    Runtime thread creation and Gateway mapping are separate writes. A
    concurrent creator can briefly expose a mapped thread before its
    fingerprint is written, so readers wait for the creator. An older
    unbound Thread is never adopted because its kernel-side tool catalog
    cannot be proven equal to the current catalog.
    """
    stored_fingerprint = str(existing.get("dynamic_tool_fingerprint") or "")
    if stored_fingerprint:
        assert_dynamic_tool_fingerprint(existing, fingerprint)
    if stored_fingerprint:
        return existing
    if not created_by_this_request:
        for _ in range(5):
            await asyncio.sleep(0.05)
            refreshed = await existing_thread(plane, tenant_id, user_id, session_id)
            if refreshed is not None and refreshed.get("dynamic_tool_fingerprint"):
                assert_dynamic_tool_fingerprint(refreshed, fingerprint)
                return refreshed
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_THREAD_RECREATE_REQUIRED", status_code=409
        )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            result = await plane.database.execute(
                """
                UPDATE assistant_runtime_threads
                   SET dynamic_tool_fingerprint = $1, updated_at = NOW()
                 WHERE runtime_thread_id = $2 AND tenant_id = $3
                   AND user_id = $4 AND session_id = $5
                   AND deleted_at IS NULL
                   AND dynamic_tool_fingerprint IS NULL
                """,
                fingerprint,
                uuid.UUID(str(existing["runtime_thread_id"])),
                tenant_id,
                user_id,
                session_id,
            )
            if str(result).endswith(" 1"):
                return {
                    **existing,
                    "dynamic_tool_fingerprint": fingerprint,
                }
        except Exception as exc:  # retry a transient mapping write
            last_error = exc
        refreshed = await existing_thread(plane, tenant_id, user_id, session_id)
        if refreshed is not None:
            assert_dynamic_tool_fingerprint(refreshed, fingerprint)
            if refreshed.get("dynamic_tool_fingerprint"):
                return refreshed
        if attempt < 2:
            await asyncio.sleep(0.05)
    error = AgentRuntimeControlError(
        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BIND_FAILED", status_code=503
    )
    if last_error is not None:
        raise error from last_error
    raise error


async def ensure_thread(
    plane: AgentRuntimeControlPlane,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    model_id: str,
    readonly_capabilities: dict[str, Any] | None = None,
    capability_allowlist: list[dict[str, Any]] | None = None,
    native_web_search_enabled: bool = False,
) -> dict[str, Any]:
    if readonly_capabilities is None:
        model = await plane.model_service.get_model(tenant_id, model_id)
        if not model or not bool(model.get("is_enabled", True)):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_MODEL_NOT_FOUND", status_code=400
            )
        capability_revision = int(model.get("capability_revision") or 1)
        readonly_capabilities = readonly_capability_payload(
            None,
            tenant_id=tenant_id,
            capability_revision=capability_revision,
        )
        await plane._fetch_capability_catalog(
            readonly_capabilities,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            model_id=model_id,
            capability_revision=capability_revision,
            capability_allowlist=capability_allowlist,
        )
    existing = await existing_thread(plane, tenant_id, user_id, session_id)
    if existing:
        fingerprint = dynamic_tool_fingerprint(readonly_capabilities or {})
        return await bind_dynamic_tool_fingerprint(
            plane,
            existing=existing,
            fingerprint=fingerprint,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
    key = (tenant_id, user_id, session_id)
    lock = plane._thread_locks.setdefault(key, asyncio.Lock())
    async with lock:
        existing = await existing_thread(plane, tenant_id, user_id, session_id)
        if existing:
            fingerprint = dynamic_tool_fingerprint(readonly_capabilities or {})
            return await bind_dynamic_tool_fingerprint(
                plane,
                existing=existing,
                fingerprint=fingerprint,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                created_by_this_request=True,
            )
        start = {
            "model": model_id,
            "modelProvider": "ai-platform-gateway",
            "cwd": "/workspace",
            # Agent emits approval requests for write-capable built-ins;
            # the Runtime broker persists and scope-binds those requests
            # before any handler is allowed to dispatch.
            "approvalPolicy": "on-request",
            "sandbox": "read-only",
            "config": runtime_model_config(
                plane.model_plane_base_url,
                model_id,
                native_web_search_enabled=native_web_search_enabled,
            ),
            "dynamicTools": dynamic_tools(readonly_capabilities or {}),
        }
        response = await plane.http_client.post(
            f"{plane.runtime_url}/internal/v1/threads",
            headers={"x-ai-platform-internal-token": plane.runtime_internal_token},
            json={
                "tenantId": tenant_id,
                "userId": user_id,
                "sessionId": session_id,
                "start": start,
            },
        )
        if response.status_code >= 400:
            # A concurrent process may have won the unique session scope.
            existing = await existing_thread(plane, tenant_id, user_id, session_id)
            if existing:
                return await bind_dynamic_tool_fingerprint(
                    plane,
                    existing=existing,
                    fingerprint=dynamic_tool_fingerprint(readonly_capabilities or {}),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                )
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_THREAD_CREATE_FAILED",
                status_code=503,
            )
        payload = response.json()
        thread = payload.get("thread") if isinstance(payload, dict) else None
        thread_id = str((thread or {}).get("id") or "")
        if not thread_id:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_THREAD_CREATE_INVALID",
                status_code=503,
            )
        fingerprint = dynamic_tool_fingerprint(readonly_capabilities or {})
        await bind_dynamic_tool_fingerprint(
            plane,
            existing={
                "runtime_thread_id": uuid.UUID(thread_id),
                "dynamic_tool_fingerprint": None,
            },
            fingerprint=fingerprint,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            created_by_this_request=True,
        )
        return {
            "runtime_thread_id": uuid.UUID(thread_id),
            "last_sequence": 0,
            "dynamic_tool_fingerprint": fingerprint,
        }


async def resume_thread(
    plane: AgentRuntimeControlPlane,
    *,
    runtime_thread_id: uuid.UUID,
    tenant_id: str,
    user_id: str,
    session_id: str,
    model_id: str,
    base_instructions: str | None = BASE_AGENT_INSTRUCTIONS_V1,
    developer_instructions: str | None = None,
    model_context_window: int | None = None,
    auto_compact_token_limit: int | None = None,
    native_web_search_enabled: bool = False,
) -> None:
    if developer_instructions is not None and not developer_instructions.strip():
        developer_instructions = GENERIC_AGENT_INSTRUCTIONS_V1
    response = await plane.http_client.post(
        f"{plane.runtime_url}/internal/v1/threads/{runtime_thread_id}/resume",
        headers={
            "x-ai-platform-internal-token": plane.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        },
        json={
            "model": model_id,
            "modelPlaneBaseUrl": plane.model_plane_base_url,
            "baseInstructions": base_instructions,
            "developerInstructions": developer_instructions,
            "modelContextWindow": model_context_window,
            "autoCompactTokenLimit": auto_compact_token_limit,
            "nativeWebSearchEnabled": native_web_search_enabled,
        },
    )
    if response.status_code >= 400:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_THREAD_RESUME_FAILED",
            status_code=503,
        )


async def verify_thread(
    plane: AgentRuntimeControlPlane,
    *,
    runtime_thread_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    model_id: str,
) -> None:
    """Verify that the durable Gateway identity is backed by a live kernel thread."""
    model = await plane.model_service.get_model(tenant_id, model_id)
    profile = model.get("effective_capabilities") if isinstance(model, dict) else None
    native_search = profile.get("native_search") if isinstance(profile, dict) else None
    tools = profile.get("tools") if isinstance(profile, dict) else None
    await resume_thread(
        plane,
        runtime_thread_id=uuid.UUID(str(runtime_thread_id)),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        native_web_search_enabled=(
            isinstance(native_search, dict)
            and native_search.get("enabled") is True
            and isinstance(tools, dict)
            and tools.get("web_search_wire") == "native"
        ),
    )


async def interrupt_turn(
    plane: AgentRuntimeControlPlane,
    *,
    runtime_thread_id: str,
    turn_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    reason: str = "client_interrupt",
) -> None:
    """Request a native kernel interrupt without switching runtimes.

    The Runtime owns the active turn state.  Gateway only authenticates
    the scope and forwards the request; it never marks a turn terminal on
    a failed dispatch, which preserves the one-call/one-result contract.
    """
    del reason  # The strict Agent turn/interrupt wire body is intentionally empty.
    response = await plane.http_client.post(
        f"{plane.runtime_url}/internal/v1/threads/{runtime_thread_id}/turns/{turn_id}/interrupt",
        headers={
            "x-ai-platform-internal-token": plane.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        },
        # Agent App Server's typed turn/interrupt request has an empty
        # body and only acknowledges after TurnAborted is emitted. The
        # public reason remains Gateway audit context; forwarding it would
        # make the strict Runtime decoder reject the interrupt.
        json={},
    )
    if response.status_code >= 400:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_INTERRUPT_FAILED",
            status_code=503 if response.status_code >= 500 else 409,
        )
    await plane._complete_run(uuid.UUID(turn_id), "cancelled")


async def start_turn(
    plane: AgentRuntimeControlPlane,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    message: str,
    model_id: str,
    reasoning_option: str | None,
    legacy_thinking_level: str | None,
    max_tokens: int | None,
    temperature: float | None = None,
    readonly_capabilities: dict[str, Any] | None = None,
    resolved_agent_snapshot: dict[str, Any] | None = None,
    developer_instructions: str | None = None,
    style_guidance: str | None = None,
    memory_mode: str = "auto",
    memory_profile: str | None = None,
    enable_dynamic_tools: bool = True,
) -> AgentTurn:
    assignment_row = await assignment(plane, tenant_id, user_id, session_id)
    model = await plane.model_service.get_model(tenant_id, model_id)
    if not model or not bool(model.get("is_enabled", True)):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_MODEL_NOT_FOUND", status_code=400
        )
    provider_id = str(model.get("provider_id") or "")
    provider = await plane.provider_service.get_provider(tenant_id, provider_id)
    if not provider or not bool(provider.get("is_enabled")):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_PROVIDER_UNAVAILABLE", status_code=503
        )
    profile = model.get("effective_capabilities")
    if not isinstance(profile, dict):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_PROFILE_INVALID", status_code=503
        )
    signed_model: dict[str, Any] = {}
    signed_agent_spec: dict[str, Any] | None = None
    if resolved_agent_snapshot is not None:
        if not isinstance(resolved_agent_snapshot, dict):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        if (
            str(resolved_agent_snapshot.get("tenant_id") or "") != tenant_id
            or str(resolved_agent_snapshot.get("user_id") or "") not in {"", user_id}
            or str(resolved_agent_snapshot.get("session_id") or "") not in {"", session_id}
            or not str(resolved_agent_snapshot.get("agent_id") or "")
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_SCOPE_MISMATCH", status_code=403
            )
        signed_model = resolved_agent_snapshot.get("model")
        signed_agent_spec = resolved_agent_snapshot.get("agent_spec")
        if not isinstance(signed_model, dict) or not isinstance(signed_agent_spec, dict):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        if (
            str(signed_model.get("id") or "") != model_id
            or str(signed_model.get("provider") or "") != provider_id
            or not isinstance(signed_agent_spec.get("developerInstructions"), str)
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_MODEL_MISMATCH", status_code=409
            )
        if not signed_agent_spec["developerInstructions"].strip():
            signed_agent_spec = {
                **signed_agent_spec,
                "developerInstructions": GENERIC_AGENT_INSTRUCTIONS_V1,
            }
    signed_parameters = signed_model.get("parameters") if signed_model else {}
    if not isinstance(signed_parameters, dict):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
        )
    effective_max_tokens = max_tokens
    signed_max_tokens = signed_parameters.get("max_tokens")
    if effective_max_tokens is None and signed_max_tokens is not None:
        if isinstance(signed_max_tokens, bool) or not isinstance(signed_max_tokens, int):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        effective_max_tokens = signed_max_tokens
    effective_temperature = temperature
    signed_temperature = signed_parameters.get("temperature")
    if effective_temperature is None and signed_temperature is not None:
        if (
            isinstance(signed_temperature, bool)
            or not isinstance(signed_temperature, int | float)
            or not 0 <= float(signed_temperature) <= 2
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        effective_temperature = float(signed_temperature)
    effective_reasoning_option = reasoning_option
    effective_legacy_thinking = legacy_thinking_level
    signed_thinking_mode = signed_parameters.get("thinking_mode")
    if (
        not effective_reasoning_option
        and not effective_legacy_thinking
        and signed_thinking_mode
    ):
        if not isinstance(signed_thinking_mode, str) or len(signed_thinking_mode) > 100:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        effective_legacy_thinking = signed_thinking_mode
    requested = effective_reasoning_option or effective_legacy_thinking or "auto"
    if developer_instructions is not None and (
        not isinstance(developer_instructions, str)
        or not developer_instructions.strip()
        or len(developer_instructions) > 256 * 1024
    ):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_AGENT_INSTRUCTIONS_INVALID", status_code=400
        )
    if (
        signed_agent_spec is not None
        and developer_instructions is not None
        and developer_instructions != signed_agent_spec["developerInstructions"]
    ):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_AGENT_INSTRUCTIONS_MISMATCH", status_code=409
        )
    agent_spec = signed_agent_spec or {
        "developerInstructions": GENERIC_AGENT_INSTRUCTIONS_V1,
        "model": {
            "id": model_id,
            "provider": provider_id,
            "parameters": {},
        },
        "knowledge": {"datasets": [], "retrieval": {}},
        "capabilities": [],
        "memory": {"mode": "session"},
    }
    if developer_instructions is not None:
        agent_spec = {**agent_spec, "developerInstructions": developer_instructions}
    if (
        signed_agent_spec is None
        and isinstance(style_guidance, str)
        and style_guidance.strip()
    ):
        combined = (
            f"{str(agent_spec['developerInstructions']).rstrip()}\n\n{style_guidance.strip()}"
        )
        if len(combined) > 256 * 1024:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_INSTRUCTIONS_INVALID", status_code=400
            )
        agent_spec = {**agent_spec, "developerInstructions": combined}
    signed_memory = agent_spec.get("memory")
    signed_memory_mode = (
        str(signed_memory.get("mode") or "session")
        if isinstance(signed_memory, dict)
        else "session"
    )
    selected_memory_mode = (
        signed_memory_mode
        if signed_agent_spec is not None
        else str(memory_mode or "auto").strip().lower()
    )
    if selected_memory_mode not in {"off", "session", "auto", "strict", "user"}:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_MEMORY_MODE_INVALID", status_code=400
        )
    signed_memory_profile = (
        signed_memory.get("profile") if isinstance(signed_memory, dict) else None
    )
    selected_memory_profile_raw = (
        signed_memory_profile
        if signed_agent_spec is not None and signed_memory_profile is not None
        else memory_profile or "basic"
    )
    if not isinstance(selected_memory_profile_raw, str):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_MEMORY_PROFILE_INVALID", status_code=400
        )
    selected_memory_profile = selected_memory_profile_raw.strip().lower()
    if selected_memory_profile not in {"off", "basic", "hybrid"}:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_MEMORY_PROFILE_INVALID", status_code=400
        )
    memory_write_allowed = (
        selected_memory_mode != "off"
        if signed_agent_spec is None
        else selected_memory_mode == "user"
    )
    memory_policy = (
        {
            "authoritative_profile": selected_memory_profile,
            "agent_memory_mode": "user",
            "memory_principal": user_id,
        }
        if memory_write_allowed
        else None
    )
    memory_context = await plane._load_memory_context(
        tenant_id=tenant_id,
        user_id=user_id,
        mode=selected_memory_mode,
    )
    capability_allowlist = snapshot_capability_allowlist(resolved_agent_snapshot)
    readonly_input = dict(readonly_capabilities or {})
    if capability_allowlist is not None:
        readonly_input["capability_allowlist"] = capability_allowlist
    if memory_context and memory_context.get("status") == "available":
        readonly_input["memory_context"] = memory_context["context"]
    resolved = resolve_reasoning_option(profile, requested)
    capability_revision = int(model.get("capability_revision") or 1)
    provider_revision = _provider_revision(provider.get("updated_at"))
    wire_protocols = profile.get("wire_protocols")
    if not isinstance(wire_protocols, dict):
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_WIRE_CAPABILITY_INVALID",
            status_code=503,
        )
    wire_protocol = str(wire_protocols.get("preferred") or "")
    supported_wires = wire_protocols.get("supported")
    if not isinstance(supported_wires, list) or wire_protocol not in supported_wires:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_WIRE_CAPABILITY_INVALID",
            status_code=503,
        )
    output_limit = min(
        int(effective_max_tokens or model.get("max_output_tokens") or 4096),
        int(model.get("max_output_tokens") or 4096),
    )
    output_limit = max(1, output_limit)
    readonly = readonly_capability_payload(
        readonly_input,
        tenant_id=tenant_id,
        capability_revision=capability_revision,
    )
    try:
        platform_config = build_runtime_platform_config(
            {
                **(resolved_agent_snapshot or {}),
                "agent_spec": agent_spec,
                "capabilities": (
                    resolved_agent_snapshot.get("capabilities", [])
                    if isinstance(resolved_agent_snapshot, dict)
                    else []
                ),
            },
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            attachment_refs=[
                str(item.get("payload", {}).get("content_ref"))
                for item in readonly.get("items", [])
                if isinstance(item, dict)
                and item.get("kind") == "attachment"
                and isinstance(item.get("payload"), dict)
                and item["payload"].get("content_ref")
            ],
        )
    except RuntimePlatformConfigError as exc:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_PLATFORM_CONFIG_INVALID", status_code=409
        ) from exc
    platform_config["config_hash"] = runtime_platform_config_hash(platform_config)
    readonly["platform_config"] = platform_config
    attach_read_attachment_descriptors(
        readonly,
        tenant_id=tenant_id,
        capability_revision=capability_revision,
    )
    if enable_dynamic_tools:
        await plane._fetch_capability_catalog(
            readonly,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            model_id=model_id,
            capability_revision=capability_revision,
            capability_allowlist=capability_allowlist,
        )
    thread = await ensure_thread(
        plane,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        readonly_capabilities=readonly,
        capability_allowlist=capability_allowlist,
        native_web_search_enabled=(
            isinstance(profile.get("native_search"), dict)
            and profile["native_search"].get("enabled") is True
            and isinstance(profile.get("tools"), dict)
            and profile["tools"].get("web_search_wire") == "native"
        ),
    )
    runtime_thread_id = uuid.UUID(str(thread["runtime_thread_id"]))
    await resume_thread(
        plane,
        runtime_thread_id=runtime_thread_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        developer_instructions=agent_spec["developerInstructions"],
        model_context_window=int(model.get("context_window") or 128000),
        auto_compact_token_limit=(
            int(model["auto_compact_token_limit"])
            if isinstance(model.get("auto_compact_token_limit"), int)
            and not isinstance(model.get("auto_compact_token_limit"), bool)
            else (
                int(profile["auto_compact_token_limit"])
                if isinstance(profile.get("auto_compact_token_limit"), int)
                and not isinstance(profile.get("auto_compact_token_limit"), bool)
                else None
            )
        ),
        native_web_search_enabled=(
            isinstance(profile.get("native_search"), dict)
            and profile["native_search"].get("enabled") is True
            and isinstance(profile.get("tools"), dict)
            and profile["tools"].get("web_search_wire") == "native"
        ),
    )
    run_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    nonce_sha256 = sha256(secrets.token_bytes(32)).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=plane.lease_ttl_seconds)
    snapshot = {
        "schema_version": "agent-runtime-snapshot/v1",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "runtime_thread_id": str(runtime_thread_id),
        "run_id": str(run_id),
        "kernel_revision": assignment_row.kernel_revision,
        "model": {
            "id": model_id,
            "provider_id": provider_id,
            "api_type": str(provider.get("api_type") or ""),
            "wire_protocol": wire_protocol,
            "provider_revision": provider_revision,
        },
        "capability_revision": capability_revision,
        "capabilities": profile,
        "instructions": {
            "developerInstructions": agent_spec["developerInstructions"],
        },
        "agent_spec": agent_spec,
        "memory": {
            "mode": selected_memory_mode,
            "context_status": (memory_context or {}).get("status", "not_loaded"),
            "policy": memory_policy,
        },
        "memory_context": memory_context,
        "reasoning": {
            "requested_option": resolved.requested,
            "effective_option": resolved.effective,
            "adapter_id": resolved.adapter_id,
            "canonical_effort": resolved.canonical_effort,
            "settings": resolved.settings,
            "fallback_reason": resolved.fallback_reason,
        },
        "input": {"message": message},
        "limits": {
            "context_window": int(model.get("context_window") or 128000),
            "max_output_tokens": output_limit,
            "max_model_calls": plane.max_model_calls,
        },
        "parameters": (
            {"temperature": effective_temperature} if effective_temperature is not None else {}
        ),
        "pricing": {
            "input_price_per_1k": float(model.get("input_price_per_1k") or 0),
            "output_price_per_1k": float(model.get("output_price_per_1k") or 0),
        },
        "tools": {
            "enabled": bool(readonly.get("tools") or readonly.get("mcp")),
            "phase": "readonly"
            if readonly.get("items") or readonly.get("tools") or readonly.get("mcp")
            else "pure_text",
        },
        "readonly_capabilities": readonly,
        "platform_config": platform_config,
        "platform_config_hash": runtime_platform_config_hash(platform_config),
    }
    # Keep compaction/model limits data-driven. Providers may omit an
    # explicit threshold; in that case the kernel uses its own bounded
    # default rather than a model-name branch.
    raw_compact_limit = model.get("auto_compact_token_limit")
    if raw_compact_limit is None:
        raw_compact_limit = profile.get("auto_compact_token_limit")
    if raw_compact_limit is not None:
        if isinstance(raw_compact_limit, bool) or not isinstance(raw_compact_limit, int):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_PROFILE_INVALID", status_code=503
            )
        snapshot["limits"]["auto_compact_token_limit"] = max(1, raw_compact_limit)
    snapshot_json = canonical_runtime_json(snapshot)
    snapshot_hash = sha256(snapshot_json.encode()).hexdigest()
    max_input_tokens = min(
        int(model.get("context_window") or 128000) * plane.max_model_calls,
        10_000_000,
    )
    max_output_tokens = min(output_limit * plane.max_model_calls, 1_000_000)
    await plane.database.fetchrow(
        """
        SELECT issue_assistant_runtime_turn(
            $1, $2, $3, $4, $5, $6, $7, $8,
            'agent-runtime-snapshot/v1', $9::jsonb, $10, $11, $12,
            $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23
        )
        """,
        snapshot_id,
        lease_id,
        run_id,
        runtime_thread_id,
        tenant_id,
        user_id,
        session_id,
        assignment_row.kernel_revision,
        snapshot_json,
        snapshot_hash,
        capability_revision,
        resolved.effective,
        RUNTIME_MODEL_LEASE_SCHEMA_VERSION,
        provider_id,
        model_id,
        provider_revision,
        nonce_sha256,
        plane.max_model_calls,
        max_input_tokens,
        max_output_tokens,
        plane.max_cost_microusd,
        expires_at,
        message,
    )
    lease_row = await plane.database.fetchrow(
        "SELECT * FROM assistant_runtime_model_leases WHERE lease_id = $1",
        lease_id,
    )
    if lease_row is None:
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_LEASE_ISSUE_FAILED", status_code=503
        )
    lease_data = dict(lease_row)
    claims = RuntimeModelLeaseClaims(
        schema_version=str(lease_data["schema_version"]),
        lease_id=str(lease_id),
        snapshot_id=str(snapshot_id),
        run_id=str(run_id),
        runtime_thread_id=str(runtime_thread_id),
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        provider_id=provider_id,
        model_id=model_id,
        capability_revision=capability_revision,
        issued_at_ms=int(lease_data["issued_at"].timestamp() * 1000),
        expires_at_ms=int(lease_data["expires_at"].timestamp() * 1000),
        nonce_sha256=nonce_sha256,
    )
    signature = plane.lease_signer.sign(claims)
    effort = resolved.canonical_effort
    if effort not in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        effort = None
    response = await plane.http_client.post(
        f"{plane.runtime_url}/internal/v1/threads/{runtime_thread_id}/turns",
        headers={
            "x-ai-platform-internal-token": plane.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        },
        json={
            "runId": str(run_id),
            "snapshotId": str(snapshot_id),
            "leaseId": str(lease_id),
            "leaseSignature": signature,
            "message": message,
            "model": model_id,
            "effort": effort,
            "capabilityRevision": capability_revision,
            "readonly": turn_prompt_readonly(readonly),
            "platformConfig": platform_config,
        },
    )
    if response.status_code >= 400:
        try:
            runtime_error = response.json().get("error")
        except (ValueError, AttributeError):
            runtime_error = None
        logger.warning(
            "Agent Runtime rejected turn start status=%s error=%s",
            response.status_code,
            str(runtime_error or "unknown")[:160],
        )
        await plane._fail_run(run_id, snapshot_id, "agent_turn_start_rejected")
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_TURN_START_FAILED", status_code=503
        )
    payload = response.json()
    returned_turn_id = str(((payload.get("turn") or {}).get("id")) or "")
    if returned_turn_id != str(run_id):
        await plane._fail_run(run_id, snapshot_id, "agent_turn_identity_mismatch")
        raise AgentRuntimeControlError(
            "AI_PLATFORM_AGENT_RUNTIME_TURN_IDENTITY_MISMATCH", status_code=503
        )
    return AgentTurn(
        runtime_thread_id=str(runtime_thread_id),
        run_id=str(run_id),
        snapshot_id=str(snapshot_id),
        lease_id=str(lease_id),
        after_sequence=int(thread.get("last_sequence") or 0),
        requested_reasoning_option=resolved.requested,
        effective_reasoning_option=resolved.effective,
        reasoning_adapter_id=resolved.adapter_id,
        capability_revision=capability_revision,
        fallback_reason=resolved.fallback_reason,
    )


__all__ = [
    "assert_dynamic_tool_fingerprint",
    "assignment",
    "bind_dynamic_tool_fingerprint",
    "cleanup_session",
    "ensure_thread",
    "existing_thread",
    "interrupt_turn",
    "resume_thread",
    "start_turn",
    "verify_thread",
]
