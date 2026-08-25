"""Gateway-owned control plane for Agent Thread and Turn lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from ai_gateway_core.agents import (
    RUNTIME_MODEL_LEASE_SCHEMA_VERSION,
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseSigner,
    canonical_runtime_json,
)
from ai_gateway_core.agents.system_prompt import (
    CORE_ASSISTANT_PROMPT,
    GENERIC_AGENT_INSTRUCTIONS,
)
from ai_gateway_core.models import resolve_reasoning_option

from .runtime_configuration import (
    RuntimePlatformConfigError,
    build_runtime_platform_config,
    runtime_platform_config_hash,
)

logger = logging.getLogger(__name__)

# Stable, provider-neutral instructions for the generic Assistant. These are
# sent through the Runtime's typed ThreadResume contract, not as user input.
BASE_AGENT_INSTRUCTIONS_V1 = CORE_ASSISTANT_PROMPT
GENERIC_AGENT_INSTRUCTIONS_V1 = GENERIC_AGENT_INSTRUCTIONS
DISCOVERY_BRIDGE_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
KERNEL_OWNED_AGENT_TOOL_ALIASES = frozenset(
    {
        # The Rust kernel owns deferred tool search/call and compaction. Keep
        # the public compatibility records, but never install a second set of
        # dynamic functions that would recurse through the Worker.
        "tool_search",
        "tool_describe",
        "tool_call",
        "context_compact",
        # The kernel exposes the native spawn_agent lifecycle.
        "spawn_subagent",
    }
)


class _Database(Protocol):
    async def fetchrow(self, query: str, *args): ...

    async def execute(self, query: str, *args): ...


@dataclass(frozen=True, slots=True)
class AgentTurn:
    runtime_thread_id: str
    run_id: str
    snapshot_id: str
    lease_id: str
    after_sequence: int
    requested_reasoning_option: str
    effective_reasoning_option: str
    reasoning_adapter_id: str
    capability_revision: int
    fallback_reason: str | None


class AgentRuntimeControlError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _project_child_runtime_event(
    envelope: dict[str, Any], parent_turn_id: str
) -> dict[str, Any] | None:
    """Project real child runs into the stable Assistant subagent vocabulary."""

    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    run_id = str(data.get("run_id") or "")
    if not run_id:
        return None
    if run_id == parent_turn_id:
        return envelope
    agent_id = str(data.get("thread_id") or "")
    if not agent_id:
        return None
    event_type = str(envelope.get("event_type") or "")
    common = {
        "agent_id": agent_id,
        "agent_type": "task",
        "call_id": run_id,
        "parent_task_id": parent_turn_id,
        "task_id": run_id,
        "session_id": data.get("session_id"),
        "thread_id": agent_id,
    }
    if event_type == "run_started":
        return {
            **envelope,
            "event_type": "subagent_started",
            "data": {
                **common,
                "description": "Delegated child task",
                "status": "running",
            },
        }
    if event_type in {"run_finished", "run_error", "cancelled"}:
        raw_status = str(data.get("status") or "failed").lower()
        status = (
            "completed"
            if event_type == "run_finished" and raw_status in {"completed", "succeeded"}
            else "cancelled"
            if raw_status == "cancelled" or event_type == "cancelled"
            else "failed"
        )
        return {
            **envelope,
            "event_type": "subagent_finished",
            "data": {**common, "status": status, "result": data.get("exit")},
        }
    if event_type == "text_delta" and isinstance(data.get("content"), str):
        return {
            **envelope,
            "event_type": "subagent_text_delta",
            "data": {**common, "content": data["content"], "status": "running"},
        }
    return None


def _provider_revision(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class AgentRuntimeControlPlane:
    def __init__(
        self,
        *,
        database: _Database,
        model_service: Any,
        provider_service: Any,
        assignment_store: Any,
        lease_signer: RuntimeModelLeaseSigner,
        runtime_url: str,
        runtime_internal_token: str,
        model_plane_base_url: str,
        kernel_revision: str,
        memory_service: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not runtime_url or not runtime_internal_token or not model_plane_base_url:
            raise ValueError("Agent Runtime control-plane endpoints and token are required")
        if not kernel_revision:
            raise ValueError("Agent Runtime kernel revision is required")
        self.database = database
        self.model_service = model_service
        self.provider_service = provider_service
        self.assignment_store = assignment_store
        self.lease_signer = lease_signer
        self.runtime_url = runtime_url.rstrip("/")
        self.runtime_internal_token = runtime_internal_token
        self.model_plane_base_url = model_plane_base_url.rstrip("/")
        self.capability_plane_url = (
            os.getenv("AI_PLATFORM_CAPABILITY_PLANE_URL", "").strip().rstrip("/")
        )
        self.kernel_revision = kernel_revision
        self.memory_service = memory_service
        if self.memory_service is None:
            with contextlib.suppress(Exception):
                from ai_gateway_core.memory import MemoryService

                self.memory_service = MemoryService(database)
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            trust_env=False,
        )
        self._owns_http_client = http_client is None
        self.lease_ttl_seconds = max(
            30,
            min(int(os.getenv("AI_PLATFORM_AGENT_RUNTIME_LEASE_TTL_SECONDS", "900")), 3600),
        )
        self.max_model_calls = max(
            1,
            min(int(os.getenv("AI_PLATFORM_AGENT_RUNTIME_MAX_MODEL_CALLS_PER_TURN", "8")), 128),
        )
        self.max_cost_microusd = max(
            1,
            int(os.getenv("AI_PLATFORM_AGENT_RUNTIME_MAX_COST_MICROUSD_PER_TURN", "5000000")),
        )
        self._thread_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def cleanup_session(
        self,
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
        response = await self.http_client.post(
            f"{self.runtime_url}/internal/v1/sessions/{quote(session_id, safe='')}/cleanup",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
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

    async def _assignment(self, tenant_id: str, user_id: str, session_id: str):
        assignment = await self.assignment_store.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
        if assignment is None or assignment.runtime_owner != "agent_runtime":
            raise AgentRuntimeControlError("AGENT_RUNTIME_ASSIGNMENT_MISMATCH", status_code=403)
        return assignment

    async def _existing_thread(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        row = await self.database.fetchrow(
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

    @staticmethod
    def _assert_dynamic_tool_fingerprint(
        existing: dict[str, Any], requested_fingerprint: str
    ) -> None:
        stored_fingerprint = str(existing.get("dynamic_tool_fingerprint") or "")
        if stored_fingerprint != requested_fingerprint:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_THREAD_RECREATE_REQUIRED", status_code=409
            )

    async def _bind_dynamic_tool_fingerprint(
        self,
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
            self._assert_dynamic_tool_fingerprint(existing, fingerprint)
        if stored_fingerprint:
            return existing
        if not created_by_this_request:
            for _ in range(5):
                await asyncio.sleep(0.05)
                refreshed = await self._existing_thread(tenant_id, user_id, session_id)
                if refreshed is not None and refreshed.get("dynamic_tool_fingerprint"):
                    self._assert_dynamic_tool_fingerprint(refreshed, fingerprint)
                    return refreshed
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_THREAD_RECREATE_REQUIRED", status_code=409
            )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = await self.database.execute(
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
            refreshed = await self._existing_thread(tenant_id, user_id, session_id)
            if refreshed is not None:
                self._assert_dynamic_tool_fingerprint(refreshed, fingerprint)
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

    def _runtime_model_config(
        self, model_id: str, *, native_web_search_enabled: bool = False
    ) -> dict[str, Any]:
        return {
            "model_provider": "ai-platform-gateway",
            "model": model_id,
            # Hosted search is exposed only when the immutable model profile
            # declares its native wire. Provider adapters still own the final
            # request serialization, so unsupported models never receive it.
            "web_search": "live" if native_web_search_enabled else "disabled",
            "features": {
                # The platform Gateway provider exposes Qwen's hosted
                # Responses search directly. Do not replace it with the
                # upstream standalone extension, which has a different auth
                # plane and would bypass the tenant model snapshot.
                "standalone_web_search": False,
                "multi_agent_v2": {
                    "enabled": True,
                    # The root thread occupies one slot; five child workers
                    # remain available without overloading local deployments.
                    "max_concurrent_threads_per_session": 6,
                }
            },
            "model_providers": {
                "ai-platform-gateway": {
                    "name": "AI Platform Gateway Model Plane",
                    "base_url": self.model_plane_base_url,
                    "env_key": "AI_PLATFORM_AGENT_RUNTIME_MODEL_PLANE_INTERNAL_TOKEN",
                    "wire_api": "responses",
                    "requires_openai_auth": False,
                    "supports_websockets": False,
                    "request_max_retries": 0,
                    "stream_max_retries": 0,
                }
            },
        }

    @staticmethod
    def _dynamic_tools(readonly: dict[str, Any]) -> list[dict[str, Any]]:
        tools = [
            *(readonly.get("tools") or []),
            *(readonly.get("mcp") or []),
            *(readonly.get("deferred") or []),
            *(readonly.get("attachment_tools") or []),
        ]
        if not isinstance(tools, list):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        result: list[dict[str, Any]] = []
        allowlist = readonly.get("capability_allowlist")
        for descriptor in tools:
            if not isinstance(descriptor, dict):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
            if descriptor.get("read_only") is not True and (
                not isinstance(allowlist, list)
                or sum(
                    1
                    for entry in allowlist
                    if isinstance(entry, dict)
                    and entry.get("id") == descriptor.get("id")
                    and entry.get("name") == descriptor.get("name")
                    and entry.get("version") == descriptor.get("version")
                    and entry.get("schema_hash") == descriptor.get("schema_hash")
                )
                != 1
            ):
                continue
            name = descriptor.get("name")
            description = descriptor.get("description")
            schema = descriptor.get("schema")
            if (
                not isinstance(name, str)
                or not isinstance(description, str)
                or not isinstance(schema, dict)
            ):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
            if name in KERNEL_OWNED_AGENT_TOOL_ALIASES:
                continue
            result.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "inputSchema": schema,
                }
            )
        return result

    @staticmethod
    def _validate_catalog_descriptor(
        descriptor: Any,
        *,
        tenant_id: str,
        capability_revision: int,
        allow_deferred: bool = False,
    ) -> dict[str, Any]:
        if (
            not isinstance(descriptor, dict)
            or (
                descriptor.get("read_only") is not True
                and not allow_deferred
            )
            or descriptor.get("tenant_id") != tenant_id
            or descriptor.get("capability_revision") != capability_revision
            or not isinstance(descriptor.get("name"), str)
            or not isinstance(descriptor.get("description"), str)
            or not isinstance(descriptor.get("schema"), dict)
            or not isinstance(descriptor.get("id"), str)
            or not descriptor.get("id")
            or not isinstance(descriptor.get("schema_hash"), str)
            or len(descriptor.get("schema_hash")) != 71
            or not descriptor.get("schema_hash").startswith("sha256:")
            or any(
                character not in "0123456789abcdef"
                for character in descriptor.get("schema_hash")[7:]
            )
            or descriptor.get("kind")
            not in {
                "tool",
                "knowledge",
                "mcp",
                "connector",
                "office_read",
                "platform_tool_discovery",
            }
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_INVALID", status_code=503
            )
        return descriptor

    @staticmethod
    def _allowlisted_catalog_descriptors(
        descriptors: list[dict[str, Any]],
        allowlist: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if allowlist is None:
            return descriptors
        allowed: list[dict[str, Any]] = []
        for descriptor in descriptors:
            matches = [
                entry
                for entry in allowlist
                if str(entry.get("id") or "") == str(descriptor.get("id") or "")
            ]
            if len(matches) != 1:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_SCOPE_MISMATCH",
                    status_code=409,
                )
            expected = matches[0]
            for field in ("version", "schema_hash"):
                expected_value = expected.get(field)
                actual_value = descriptor.get(field)
                allow_platform_schema_resolution = (
                    field == "schema_hash"
                    and expected.get("type") in {"platform", "connector"}
                    and expected_value is None
                )
                if actual_value != expected_value and not allow_platform_schema_resolution:
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_SCOPE_MISMATCH",
                        status_code=409,
                    )
            allowed.append(descriptor)
        return allowed

    @staticmethod
    def _snapshot_capability_allowlist(
        snapshot: dict[str, Any] | None,
    ) -> list[dict[str, Any]] | None:
        """Project signed AgentSpec capabilities into a catalog allowlist.

        Connector credentials are deliberately not part of this projection.
        The resolver has already authorized the binding while building the
        Agent snapshot, so only the non-secret principal identity is retained
        for the worker's later, request-time revocation check.  In particular,
        the channel comes from the signed AgentSpec and cannot be overridden by
        connector config.
        """

        if snapshot is None:
            return None
        raw_capabilities = snapshot.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
            )
        allowlist: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for raw in raw_capabilities:
            if not isinstance(raw, dict):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
                )
            capability_type = str(raw.get("type") or "")
            capability_id = str(raw.get("id") or "")
            config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
            name = str(config.get("tool_name") or config.get("name") or capability_id)
            version = str(raw.get("version") or "")
            schema_hash = str(raw.get("schema_hash") or "")
            if not capability_type or not capability_id or not name:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_AGENT_SNAPSHOT_INVALID", status_code=409
                )
            if not schema_hash and capability_type not in {"platform", "connector"}:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                )
            key = (capability_type, capability_id, version, schema_hash)
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "type": capability_type,
                "name": name,
                "id": capability_id,
                "version": version or None,
                "schema_hash": schema_hash or None,
            }
            if capability_type == "mcp":
                if not isinstance(raw.get("config"), dict) or set(config) - {
                    "connection_id",
                    "principal_type",
                    "risk",
                }:
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                connection_id = str(config.get("connection_id") or "")
                principal_type = str(config.get("principal_type") or "")
                risk_level = str(config.get("risk") or raw.get("risk") or "")
                agent_spec = snapshot.get("agent_spec")
                channel = agent_spec.get("channel") if isinstance(agent_spec, dict) else None
                try:
                    uuid.UUID(connection_id)
                except (ValueError, AttributeError, TypeError):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    ) from None
                if (
                    principal_type not in {"service_account", "user_delegated"}
                    or risk_level not in {"low", "medium", "high", "critical"}
                    or channel
                    not in {"preview", "hosted_private", "hosted_public", "embed", "api"}
                    or len(schema_hash) != 71
                    or not schema_hash.startswith("sha256:")
                ):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                entry["connector_binding"] = {
                    "binding_type": "grant",
                    "provider": "mcp",
                    "tool_name": name,
                    "principal_type": principal_type,
                    "grant_id": None,
                    "connection_id": connection_id,
                    "schema_hash": schema_hash,
                    "risk_level": risk_level,
                    "channel": channel,
                }
            elif capability_type == "connector":
                if not isinstance(raw.get("config"), dict):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                connector_config = raw["config"]
                if set(connector_config) - {
                    "provider",
                    "tool_name",
                    "principal_type",
                    "grant_id",
                }:
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                provider = connector_config.get("provider")
                tool_name = connector_config.get("tool_name", name)
                principal_type = connector_config.get("principal_type")
                grant_id = connector_config.get("grant_id")
                agent_spec = snapshot.get("agent_spec")
                channel = agent_spec.get("channel") if isinstance(agent_spec, dict) else None
                if (
                    not isinstance(provider, str)
                    or not provider
                    or len(provider) > 128
                    or not isinstance(tool_name, str)
                    or tool_name != name
                    or not isinstance(channel, str)
                    or channel
                    not in {
                        "preview",
                        "hosted",
                        "hosted_private",
                        "hosted_public",
                        "embed",
                        "api",
                        "builtin",
                    }
                ):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                if (principal_type is None) != (grant_id is None):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                if principal_type is not None and (
                    principal_type not in {"service_account", "user_delegated"}
                    or not isinstance(grant_id, str)
                    or not grant_id
                ):
                    raise AgentRuntimeControlError(
                        "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                    )
                if grant_id is not None:
                    try:
                        uuid.UUID(grant_id)
                    except (ValueError, AttributeError, TypeError):
                        raise AgentRuntimeControlError(
                            "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                        ) from None
                entry["connector_binding"] = {
                    "binding_type": "grant" if grant_id is not None else "catalog",
                    "provider": provider,
                    "tool_name": tool_name,
                    "principal_type": principal_type,
                    "grant_id": grant_id,
                    "channel": channel,
                }
            allowlist.append(entry)
        return allowlist

    async def ensure_thread(
        self,
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
            model = await self.model_service.get_model(tenant_id, model_id)
            if not model or not bool(model.get("is_enabled", True)):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_MODEL_NOT_FOUND", status_code=400
                )
            capability_revision = int(model.get("capability_revision") or 1)
            readonly_capabilities = self._readonly_capability_payload(
                None,
                tenant_id=tenant_id,
                capability_revision=capability_revision,
            )
            await self._fetch_capability_catalog(
                readonly_capabilities,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                capability_revision=capability_revision,
                capability_allowlist=capability_allowlist,
            )
        existing = await self._existing_thread(tenant_id, user_id, session_id)
        if existing:
            fingerprint = self._dynamic_tool_fingerprint(readonly_capabilities or {})
            return await self._bind_dynamic_tool_fingerprint(
                existing=existing,
                fingerprint=fingerprint,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
            )
        key = (tenant_id, user_id, session_id)
        lock = self._thread_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = await self._existing_thread(tenant_id, user_id, session_id)
            if existing:
                fingerprint = self._dynamic_tool_fingerprint(readonly_capabilities or {})
                return await self._bind_dynamic_tool_fingerprint(
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
                "config": self._runtime_model_config(
                    model_id,
                    native_web_search_enabled=native_web_search_enabled,
                ),
                "dynamicTools": self._dynamic_tools(readonly_capabilities or {}),
            }
            response = await self.http_client.post(
                f"{self.runtime_url}/internal/v1/threads",
                headers={"x-ai-platform-internal-token": self.runtime_internal_token},
                json={
                    "tenantId": tenant_id,
                    "userId": user_id,
                    "sessionId": session_id,
                    "start": start,
                },
            )
            if response.status_code >= 400:
                # A concurrent process may have won the unique session scope.
                existing = await self._existing_thread(tenant_id, user_id, session_id)
                if existing:
                    return await self._bind_dynamic_tool_fingerprint(
                        existing=existing,
                        fingerprint=self._dynamic_tool_fingerprint(readonly_capabilities or {}),
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
            fingerprint = self._dynamic_tool_fingerprint(readonly_capabilities or {})
            await self._bind_dynamic_tool_fingerprint(
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

    @staticmethod
    def _dynamic_tool_fingerprint(readonly: dict[str, Any]) -> str:
        # Attachment refs are turn-scoped data inputs. Their read_attachment
        # descriptor is rebuilt from the normalized refs for each turn and is
        # authorized by the immutable snapshot/Worker lease; pinning that
        # ephemeral descriptor to the Thread would make equivalent attachment
        # normalization spuriously require Thread recreation.
        tools = [
            *(readonly.get("tools") or []),
            *(readonly.get("mcp") or []),
            *(readonly.get("deferred") or []),
        ]
        return sha256(canonical_runtime_json(tools).encode()).hexdigest()

    @staticmethod
    def _attachment_tool_descriptor(
        *, tenant_id: str, capability_revision: int, references: list[str]
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "enum": references},
                "offset": {"type": "integer", "minimum": 0, "maximum": 2_000_000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8_000},
            },
            "required": ["ref"],
            "additionalProperties": False,
        }
        schema_hash = "sha256:" + sha256(canonical_runtime_json(schema).encode()).hexdigest()
        return {
            "name": "read_attachment",
            "description": "Read a bounded slice from an explicitly attached artifact reference.",
            "schema": schema,
            "tenant_id": tenant_id,
            "capability_revision": capability_revision,
            "source": "attachments",
            "kind": "tool",
            "category": "retrieval",
            "protocol": "internal",
            "id": "read_attachment",
            "version": "v1",
            "schema_hash": schema_hash,
            "read_only": True,
            "metadata": {"effect": "read", "attachment_refs": references},
        }

    @classmethod
    def _attach_read_attachment_descriptors(
        cls,
        readonly: dict[str, Any],
        *,
        tenant_id: str,
        capability_revision: int,
    ) -> None:
        refs = [
            str(item["payload"].get("content_ref"))
            for item in readonly.get("items", [])
            if isinstance(item, dict)
            and item.get("kind") == "attachment"
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("content_ref")
        ]
        references = sorted(set(refs))
        descriptors = (
            [
                cls._attachment_tool_descriptor(
                    tenant_id=tenant_id,
                    capability_revision=capability_revision,
                    references=references,
                )
            ]
            if references
            else []
        )
        readonly["attachment_tools"] = descriptors
        if descriptors:
            entries = list(readonly.get("capability_allowlist") or [])
            for descriptor in descriptors:
                entries.append(
                    {
                        "type": "platform",
                        "name": descriptor["name"],
                        "id": descriptor["id"],
                        "version": descriptor["version"],
                        "schema_hash": descriptor["schema_hash"],
                    }
                )
            readonly["capability_allowlist"] = entries

    @staticmethod
    def _worker_ready_for_writes() -> bool:
        return (
            os.getenv("AI_PLATFORM_CAPABILITY_WORKER_ENABLED", "").lower() == "true"
            and os.getenv("AI_PLATFORM_CAPABILITY_WORKER_WRITES_ENABLED", "").lower() == "true"
            and bool(os.getenv("AI_PLATFORM_CAPABILITY_WORKER_URL", "").strip())
            and bool(os.getenv("AI_PLATFORM_CAPABILITY_LEASE_SIGNING_SECRET", "").strip())
        )

    async def _resume_thread(
        self,
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
        response = await self.http_client.post(
            f"{self.runtime_url}/internal/v1/threads/{runtime_thread_id}/resume",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
                "x-ai-tenant-id": tenant_id,
                "x-ai-user-id": user_id,
                "x-ai-session-id": session_id,
            },
            json={
                "model": model_id,
                "modelPlaneBaseUrl": self.model_plane_base_url,
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
        self,
        *,
        runtime_thread_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        model_id: str,
    ) -> None:
        """Verify that the durable Gateway identity is backed by a live kernel thread."""
        model = await self.model_service.get_model(tenant_id, model_id)
        profile = model.get("effective_capabilities") if isinstance(model, dict) else None
        native_search = profile.get("native_search") if isinstance(profile, dict) else None
        tools = profile.get("tools") if isinstance(profile, dict) else None
        await self._resume_thread(
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

    @staticmethod
    def _readonly_capability_payload(
        value: dict[str, Any] | None,
        *,
        tenant_id: str,
        capability_revision: int,
    ) -> dict[str, Any]:
        """Normalize explicit read-only references into the runtime contract.

        This adapter accepts platform-selected references only. It does not
        inspect user text and it never carries a write-capable tool schema.
        """

        raw = value if isinstance(value, dict) else {}
        allowed = {
            "knowledge",
            "attachments",
            "web_search",
            "tools",
            "mcp",
            "deferred",
            "memory_context",
            "capability_allowlist",
            "platform_config",
            "attachment_tools",
            "responses_tool_names",
            "responses_tool_choice",
            "responses_parallel_tool_calls",
        }
        if set(raw) - allowed:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        items: list[dict[str, Any]] = []
        knowledge = raw.get("knowledge")
        if knowledge is not None and not isinstance(knowledge, dict):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        if isinstance(knowledge, dict):
            dataset_ids = knowledge.get("dataset_ids") or []
            if not isinstance(dataset_ids, list) or any(
                not isinstance(item, str) for item in dataset_ids
            ):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
            for dataset_id in dataset_ids:
                items.append(
                    {
                        "item_id": f"knowledge:{dataset_id}",
                        "kind": "knowledge",
                        "source": "knowledge",
                        "payload": {"dataset_id": dataset_id},
                        "tenant_id": tenant_id,
                        "capability_revision": capability_revision,
                    }
                )
        attachments = raw.get("attachments")
        if attachments is not None and not isinstance(attachments, dict):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        if isinstance(attachments, dict):
            refs = attachments.get("refs") or []
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
            for reference in refs:
                items.append(
                    {
                        "item_id": f"attachment:{reference}",
                        "kind": "attachment",
                        "source": "attachments",
                        "payload": {"content_ref": reference},
                        "tenant_id": tenant_id,
                        "capability_revision": capability_revision,
                    }
                )
        web_search = raw.get("web_search")
        if web_search is not None and not isinstance(web_search, dict):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        if isinstance(web_search, dict) and web_search.get("enabled"):
            max_results = web_search.get("max_results") or 5
            if isinstance(max_results, bool):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
            try:
                max_results = int(max_results)
            except (TypeError, ValueError) as exc:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                ) from exc
            items.append(
                {
                    "item_id": "context:web-search",
                    "kind": "context",
                    "source": "web-search",
                    "payload": {"max_results": max_results},
                    "tenant_id": tenant_id,
                    "capability_revision": capability_revision,
                }
            )
        memory_context = raw.get("memory_context")
        if memory_context is not None:
            if not isinstance(memory_context, dict):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_MEMORY_CONTEXT_INVALID", status_code=503
                )
            items.append(
                {
                    "item_id": "context:long-term-memory",
                    "kind": "context",
                    "source": "long-term-memory",
                    "payload": memory_context,
                    "tenant_id": tenant_id,
                    "capability_revision": capability_revision,
                }
            )
        tools = raw.get("tools") or []
        mcp = raw.get("mcp") or []
        if not isinstance(tools, list) or not isinstance(mcp, list):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        deferred = raw.get("deferred") or []
        if not isinstance(deferred, list):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        for descriptor in [*tools, *mcp]:
            if (
                not isinstance(descriptor, dict)
                or descriptor.get("read_only") is not True
                or descriptor.get("tenant_id") != tenant_id
                or int(descriptor.get("capability_revision") or 0) != capability_revision
            ):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
        attachment_tools = raw.get("attachment_tools") or []
        if not isinstance(attachment_tools, list):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        for descriptor in attachment_tools:
            if (
                not isinstance(descriptor, dict)
                or descriptor.get("read_only") is not True
                or descriptor.get("tenant_id") != tenant_id
                or int(descriptor.get("capability_revision") or 0) != capability_revision
                or descriptor.get("name") != "read_attachment"
            ):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
        for descriptor in deferred:
            if (
                not isinstance(descriptor, dict)
                or descriptor.get("read_only") is True
                or descriptor.get("tenant_id") != tenant_id
                or int(descriptor.get("capability_revision") or 0) != capability_revision
            ):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
                )
        responses_tool_names = raw.get("responses_tool_names")
        if responses_tool_names is not None and (
            not isinstance(responses_tool_names, list)
            or len(responses_tool_names) > 128
            or any(not isinstance(name, str) or not name.strip() for name in responses_tool_names)
            or len(set(responses_tool_names)) != len(responses_tool_names)
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        responses_tool_choice = raw.get("responses_tool_choice", "auto")
        if not (
            isinstance(responses_tool_choice, str)
            and responses_tool_choice in {"auto", "none", "required"}
        ) and not (
            isinstance(responses_tool_choice, dict)
            and responses_tool_choice.get("type") == "function"
            and isinstance(responses_tool_choice.get("name"), str)
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        responses_parallel_tool_calls = raw.get("responses_parallel_tool_calls", True)
        if not isinstance(responses_parallel_tool_calls, bool):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        capability_allowlist = raw.get("capability_allowlist")
        if capability_allowlist is not None and (
            not isinstance(capability_allowlist, list)
            or any(not isinstance(item, dict) for item in capability_allowlist)
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400
            )
        normalized = {
            "schema_version": "agent-readonly-capability/v1",
            "tenant_id": tenant_id,
            "capability_revision": capability_revision,
            "items": items,
            "tools": tools,
            "mcp": mcp,
            "deferred": deferred,
            "attachment_tools": attachment_tools,
            "responses_tool_names": responses_tool_names,
            "responses_tool_choice": responses_tool_choice,
            "responses_parallel_tool_calls": responses_parallel_tool_calls,
        }
        platform_config = raw.get("platform_config")
        if platform_config is not None:
            if not isinstance(platform_config, dict):
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_PLATFORM_CONFIG_INVALID", status_code=409
                )
            normalized["platform_config"] = platform_config
        if capability_allowlist is not None:
            normalized["capability_allowlist"] = capability_allowlist
        return normalized

    async def _fetch_capability_catalog(
        self,
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

        if not self.capability_plane_url:
            return
        response = await self.http_client.post(
            f"{self.capability_plane_url}/catalog",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
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
        if deferred and not self._worker_ready_for_writes():
            if capability_allowlist is not None:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_WRITE_CAPABILITY_NOT_MIGRATED",
                    status_code=409,
                )
            deferred = []
        readonly["tools"] = [
            self._validate_catalog_descriptor(
                descriptor,
                tenant_id=tenant_id,
                capability_revision=capability_revision,
            )
            for descriptor in tools
        ]
        readonly["mcp"] = [
            self._validate_catalog_descriptor(
                descriptor,
                tenant_id=tenant_id,
                capability_revision=capability_revision,
            )
            for descriptor in mcp
        ]
        deferred = [
            self._validate_catalog_descriptor(
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
        allowed_tools = self._allowlisted_catalog_descriptors(bound_tools, capability_allowlist)
        allowed_mcp = self._allowlisted_catalog_descriptors(mcp, capability_allowlist)
        allowed_deferred = (
            self._allowlisted_catalog_descriptors(deferred, capability_allowlist)
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

    async def _load_memory_context(
        self,
        *,
        tenant_id: str,
        user_id: str,
        mode: str,
    ) -> dict[str, Any] | None:
        """Load bounded cross-session memory only for an explicit memory mode."""

        if mode not in {"auto", "strict", "user"}:
            return None
        service = self.memory_service
        if service is None or not hasattr(service, "get_long_term_context"):
            if mode in {"strict", "user"}:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_MEMORY_UNAVAILABLE", status_code=503
                )
            return {"status": "unavailable", "reason": "memory_service_unavailable"}
        try:
            context = await service.get_long_term_context(
                tenant_id=tenant_id,
                user_id=user_id,
                limit=20,
            )
        except Exception as exc:  # noqa: BLE001 - memory policy decides fallback
            if mode in {"strict", "user"}:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_MEMORY_UNAVAILABLE", status_code=503
                ) from exc
            return {"status": "unavailable", "reason": "memory_lookup_failed"}
        if not isinstance(context, dict):
            if mode in {"strict", "user"}:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_MEMORY_INVALID", status_code=503
                )
            return {"status": "unavailable", "reason": "memory_payload_invalid"}
        preferences = context.get("preferences")
        frequent = context.get("frequent_memories")
        if not isinstance(preferences, dict):
            preferences = {}
        if not isinstance(frequent, list):
            frequent = []
        bounded_frequent: list[dict[str, Any]] = []
        for item in frequent[:10]:
            if not isinstance(item, dict):
                continue
            bounded_frequent.append(
                {
                    "key": str(item.get("key") or "")[:128],
                    "value": item.get("value"),
                    "access_count": int(item.get("access_count") or 0),
                }
            )
        bounded = {"preferences": preferences, "frequent_memories": bounded_frequent}
        encoded = canonical_runtime_json(bounded)
        if len(encoded) > 64 * 1024:
            bounded["frequent_memories"] = []
            encoded = canonical_runtime_json(bounded)
            if len(encoded) > 64 * 1024:
                bounded["preferences"] = {}
        return {"status": "available", "context": bounded}

    async def start_turn(
        self,
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
        memory_mode: str = "auto",
        memory_profile: str | None = None,
        enable_dynamic_tools: bool = True,
    ) -> AgentTurn:
        assignment = await self._assignment(tenant_id, user_id, session_id)
        model = await self.model_service.get_model(tenant_id, model_id)
        if not model or not bool(model.get("is_enabled", True)):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_MODEL_NOT_FOUND", status_code=400
            )
        provider_id = str(model.get("provider_id") or "")
        provider = await self.provider_service.get_provider(tenant_id, provider_id)
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
        memory_context = await self._load_memory_context(
            tenant_id=tenant_id,
            user_id=user_id,
            mode=selected_memory_mode,
        )
        capability_allowlist = self._snapshot_capability_allowlist(resolved_agent_snapshot)
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
        readonly = self._readonly_capability_payload(
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
        self._attach_read_attachment_descriptors(
            readonly,
            tenant_id=tenant_id,
            capability_revision=capability_revision,
        )
        if enable_dynamic_tools:
            await self._fetch_capability_catalog(
                readonly,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                model_id=model_id,
                capability_revision=capability_revision,
                capability_allowlist=capability_allowlist,
            )
        thread = await self.ensure_thread(
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
        await self._resume_thread(
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
        expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        snapshot = {
            "schema_version": "agent-runtime-snapshot/v1",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "runtime_thread_id": str(runtime_thread_id),
            "run_id": str(run_id),
            "kernel_revision": assignment.kernel_revision,
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
                "max_model_calls": self.max_model_calls,
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
            int(model.get("context_window") or 128000) * self.max_model_calls,
            10_000_000,
        )
        max_output_tokens = min(output_limit * self.max_model_calls, 1_000_000)
        await self.database.fetchrow(
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
            assignment.kernel_revision,
            snapshot_json,
            snapshot_hash,
            capability_revision,
            resolved.effective,
            RUNTIME_MODEL_LEASE_SCHEMA_VERSION,
            provider_id,
            model_id,
            provider_revision,
            nonce_sha256,
            self.max_model_calls,
            max_input_tokens,
            max_output_tokens,
            self.max_cost_microusd,
            expires_at,
            message,
        )
        lease_row = await self.database.fetchrow(
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
        signature = self.lease_signer.sign(claims)
        effort = resolved.canonical_effort
        if effort not in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            effort = None
        response = await self.http_client.post(
            f"{self.runtime_url}/internal/v1/threads/{runtime_thread_id}/turns",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
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
                "readonly": readonly,
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
            await self._fail_run(run_id, snapshot_id, "agent_turn_start_rejected")
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_TURN_START_FAILED", status_code=503
            )
        payload = response.json()
        returned_turn_id = str(((payload.get("turn") or {}).get("id")) or "")
        if returned_turn_id != str(run_id):
            await self._fail_run(run_id, snapshot_id, "agent_turn_identity_mismatch")
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

    async def stream_events(
        self,
        *,
        turn: AgentTurn,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> AsyncIterator[bytes]:
        url = f"{self.runtime_url}/internal/v1/threads/{turn.runtime_thread_id}/events"
        headers = {
            "x-ai-platform-internal-token": self.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        }
        terminal_status: str | None = None
        async with self.http_client.stream(
            "GET",
            url,
            headers=headers,
            params={"after_sequence": turn.after_sequence, "limit": 1000},
        ) as response:
            if response.status_code >= 400:
                await self._fail_run(
                    uuid.UUID(turn.run_id),
                    uuid.UUID(turn.snapshot_id),
                    "agent_event_stream_rejected",
                )
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_EVENT_STREAM_FAILED", status_code=503
                )
            frame: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    frame.append(line)
                    continue
                if not frame:
                    continue
                current_frame = frame
                frame = []
                encoded = ("\n".join(current_frame) + "\n\n").encode()
                event_type = next(
                    (value[6:].strip() for value in current_frame if value.startswith("event:")),
                    "",
                )
                data_raw = next(
                    (value[5:].strip() for value in current_frame if value.startswith("data:")),
                    "",
                )
                if data_raw:
                    with contextlib.suppress(json.JSONDecodeError):
                        event = json.loads(data_raw)
                        event_data = event.get("data") if isinstance(event, dict) else None
                        if (
                            isinstance(event_data, dict)
                            and str(event_data.get("run_id") or "") == turn.run_id
                        ):
                            if event_type == "run_started":
                                event_data.update(
                                    {
                                        "requested_reasoning_option": turn.requested_reasoning_option,
                                        "effective_reasoning_option": turn.effective_reasoning_option,
                                        "reasoning_adapter_id": turn.reasoning_adapter_id,
                                        "capability_revision": turn.capability_revision,
                                        "reasoning_fallback_reason": turn.fallback_reason,
                                        "kernel": "agent",
                                    }
                                )
                                encoded_data = json.dumps(
                                    event,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                encoded = (
                                    "\n".join(
                                        f"data: {encoded_data}"
                                        if value.startswith("data:")
                                        else value
                                        for value in current_frame
                                    )
                                    + "\n\n"
                                ).encode()
                            if event_type in {"run_finished", "run_error"}:
                                terminal_status = str(event_data.get("status") or "failed")
                yield encoded
                if terminal_status:
                    break
        if terminal_status:
            await self._complete_run(uuid.UUID(turn.run_id), terminal_status)

    async def stream_thread_events(
        self,
        *,
        runtime_thread_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 1000,
        turn_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream Runtime replay + live broadcast without Gateway DB polling."""
        url = f"{self.runtime_url}/internal/v1/threads/{runtime_thread_id}/events"
        headers = {
            "x-ai-platform-internal-token": self.runtime_internal_token,
            "x-ai-tenant-id": tenant_id,
            "x-ai-user-id": user_id,
            "x-ai-session-id": session_id,
        }
        frame: list[str] = []
        terminal_status: str | None = None
        async with self.http_client.stream(
            "GET",
            url,
            headers=headers,
            params={
                "after_sequence": max(0, int(after_sequence)),
                "limit": max(1, min(int(limit), 1000)),
            },
        ) as response:
            if response.status_code >= 400:
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_EVENT_STREAM_FAILED", status_code=503
                )
            async for line in response.aiter_lines():
                if line:
                    frame.append(line)
                    continue
                if not frame:
                    continue
                data_raw = next(
                    (value[5:].strip() for value in frame if value.startswith("data:")), ""
                )
                frame = []
                if not data_raw:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    envelope = json.loads(data_raw)
                    if not isinstance(envelope, dict):
                        continue
                    event_data = envelope.get("data")
                    if turn_id:
                        projected = _project_child_runtime_event(envelope, turn_id)
                        if projected is None:
                            continue
                        envelope = projected
                        event_data = envelope.get("data")
                    event_type = str(envelope.get("event_type") or "")
                    yield envelope
                    if event_type in {"run_finished", "run_error"}:
                        terminal_status = str((event_data or {}).get("status") or "failed")
                        break
        if terminal_status and turn_id:
            await self._complete_run(uuid.UUID(turn_id), terminal_status)

    async def interrupt_turn(
        self,
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
        response = await self.http_client.post(
            f"{self.runtime_url}/internal/v1/threads/{runtime_thread_id}/turns/{turn_id}/interrupt",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
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
        await self._complete_run(uuid.UUID(turn_id), "cancelled")

    async def get_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        response = await self.http_client.get(
            f"{self.runtime_url}/internal/v1/approvals/{approval_id}",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
                "x-ai-tenant-id": tenant_id,
                "x-ai-user-id": user_id,
                "x-ai-session-id": session_id,
            },
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_LOOKUP_FAILED", status_code=503
            )
        payload = response.json()
        return payload if isinstance(payload, dict) else None

    async def decide_approval(
        self,
        *,
        approval_id: str,
        approved: bool,
        reason: str | None = None,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        response = await self.http_client.post(
            f"{self.runtime_url}/internal/v1/approvals/{approval_id}/decision",
            headers={
                "x-ai-platform-internal-token": self.runtime_internal_token,
                "x-ai-tenant-id": tenant_id,
                "x-ai-user-id": user_id,
                "x-ai-session-id": session_id,
            },
            json={
                "decision": "approve" if approved else "reject",
                "reason": reason,
            },
        )
        if response.status_code >= 400:
            status = 409 if response.status_code in {400, 404, 409} else 503
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_APPROVAL_DECISION_FAILED", status_code=status
            )
        payload = response.json()
        return (
            payload
            if isinstance(payload, dict)
            else {"approval_id": approval_id, "status": "consumed"}
        )

    async def _complete_run(self, run_id: uuid.UUID, terminal_status: str) -> None:
        status = (
            "succeeded"
            if terminal_status == "succeeded"
            else "cancelled"
            if terminal_status == "cancelled"
            else "failed"
        )
        usage = await self.database.fetchrow(
            """
            SELECT COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
                   COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
                   COALESCE(SUM(cost_microusd), 0)::bigint AS cost_microusd
              FROM assistant_runtime_model_calls
             WHERE run_id = $1 AND status = 'completed'
            """,
            run_id,
        )
        await self.database.execute(
            """
            UPDATE assistant_runtime_model_leases
               SET status = 'revoked', revoked_at = NOW(),
                   revoked_reason = $2, updated_at = NOW()
             WHERE run_id = $1 AND status = 'active'
            """,
            run_id,
            f"turn_{status}",
        )
        await self.database.execute(
            """
            UPDATE assistant_runs
               SET status = $2, usage = $3::jsonb, finished_at = NOW(), updated_at = NOW()
             WHERE run_id = $1 AND engine = 'agent_runtime' AND status = 'running'
            """,
            run_id,
            status,
            json.dumps(dict(usage or {}), separators=(",", ":")),
        )

    async def _fail_run(
        self,
        run_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        reason: str,
    ) -> None:
        await self.database.execute(
            """
            UPDATE assistant_runtime_model_leases
               SET status = 'revoked', revoked_at = NOW(), revoked_reason = $3, updated_at = NOW()
             WHERE run_id = $1 AND snapshot_id = $2 AND status = 'active'
            """,
            run_id,
            snapshot_id,
            reason,
        )
        await self.database.execute(
            """
            UPDATE assistant_runs
               SET status = 'failed', error = $2, finished_at = NOW(), updated_at = NOW()
             WHERE run_id = $1 AND engine = 'agent_runtime' AND status = 'running'
            """,
            run_id,
            reason,
        )


__all__ = [
    "AgentTurn",
    "AgentRuntimeControlError",
    "AgentRuntimeControlPlane",
]
