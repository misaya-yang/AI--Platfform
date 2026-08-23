"""Gateway-owned control plane for Agent Thread and Turn lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Protocol

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

# Stable, provider-neutral instructions for the generic Assistant. These are
# sent through the Runtime's typed ThreadResume contract, not as user input.
BASE_AGENT_INSTRUCTIONS_V1 = CORE_ASSISTANT_PROMPT
GENERIC_AGENT_INSTRUCTIONS_V1 = GENERIC_AGENT_INSTRUCTIONS
DISCOVERY_BRIDGE_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
KERNEL_OWNED_AGENT_TOOL_ALIASES = frozenset({"spawn_subagent"})


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
        self.capability_plane_url = os.getenv("AI_PLATFORM_CAPABILITY_PLANE_URL", "").strip().rstrip("/")
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

    def _runtime_model_config(self, model_id: str) -> dict[str, Any]:
        return {
            "model_provider": "ai-platform-gateway",
            "model": model_id,
            "features": {
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
        tools = [*(readonly.get("tools") or []), *(readonly.get("mcp") or [])]
        if not isinstance(tools, list):
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        result: list[dict[str, Any]] = []
        for descriptor in tools:
            if not isinstance(descriptor, dict) or descriptor.get("read_only") is not True:
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
            name = descriptor.get("name")
            description = descriptor.get("description")
            schema = descriptor.get("schema")
            if not isinstance(name, str) or not isinstance(description, str) or not isinstance(schema, dict):
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
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
        descriptor: Any, *, tenant_id: str, capability_revision: int
    ) -> dict[str, Any]:
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("read_only") is not True
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
            or descriptor.get("kind") not in {
                "tool",
                "knowledge",
                "mcp",
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
                    and expected.get("type") == "platform"
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
        """Project signed AgentSpec capabilities into a catalog allowlist."""

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
            if not schema_hash and capability_type != "platform":
                raise AgentRuntimeControlError(
                    "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_BINDING_INVALID", status_code=409
                )
            key = (capability_type, capability_id, version, schema_hash)
            if key in seen:
                continue
            seen.add(key)
            allowlist.append(
                {
                    "type": capability_type,
                    "name": name,
                    "id": capability_id,
                    "version": version or None,
                    "schema_hash": schema_hash or None,
                }
            )
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
                "config": self._runtime_model_config(model_id),
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
                        fingerprint=self._dynamic_tool_fingerprint(
                            readonly_capabilities or {}
                        ),
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
        tools = [*(readonly.get("tools") or []), *(readonly.get("mcp") or [])]
        return sha256(canonical_runtime_json(tools).encode()).hexdigest()

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
        await self._resume_thread(
            runtime_thread_id=uuid.UUID(str(runtime_thread_id)),
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            model_id=model_id,
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
            "memory_context",
            "capability_allowlist",
        }
        if set(raw) - allowed:
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        items: list[dict[str, Any]] = []
        knowledge = raw.get("knowledge")
        if knowledge is not None and not isinstance(knowledge, dict):
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        if isinstance(knowledge, dict):
            dataset_ids = knowledge.get("dataset_ids") or []
            if not isinstance(dataset_ids, list) or any(not isinstance(item, str) for item in dataset_ids):
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
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
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        if isinstance(attachments, dict):
            refs = attachments.get("refs") or []
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
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
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        if isinstance(web_search, dict) and web_search.get("enabled"):
            max_results = web_search.get("max_results") or 5
            if isinstance(max_results, bool):
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
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
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
        for descriptor in [*tools, *mcp]:
            if (
                not isinstance(descriptor, dict)
                or descriptor.get("read_only") is not True
                or descriptor.get("tenant_id") != tenant_id
                or int(descriptor.get("capability_revision") or 0) != capability_revision
            ):
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_READONLY_PAYLOAD_INVALID", status_code=400)
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
        }
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
        if capability_allowlist is not None and deferred:
            # The dynamic Runtime bridge is deliberately read-only. Refuse an
            # AgentSpec that binds write/unknown capabilities until its typed
            # approval contributor is available instead of silently dropping
            # requested authority and producing misleading Agent behavior.
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_WRITE_CAPABILITY_NOT_MIGRATED",
                status_code=409,
            )
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
        bridges = [
            item for item in readonly["tools"] if item.get("name") in DISCOVERY_BRIDGE_NAMES
        ]
        bound_tools = [
            item for item in readonly["tools"] if item.get("name") not in DISCOVERY_BRIDGE_NAMES
        ]
        allowed_tools = self._allowlisted_catalog_descriptors(
            bound_tools, capability_allowlist
        )
        allowed_mcp = self._allowlisted_catalog_descriptors(
            readonly["mcp"], capability_allowlist
        )
        readonly["tools"] = allowed_tools + bridges
        readonly["mcp"] = allowed_mcp
        if capability_allowlist is not None and len(allowed_tools) + len(allowed_mcp) != len(
            capability_allowlist
        ):
            raise AgentRuntimeControlError(
                "AI_PLATFORM_AGENT_RUNTIME_CAPABILITY_CATALOG_SCOPE_MISMATCH",
                status_code=409,
            )
        # Every turn, including the generic Assistant without an AgentSpec,
        # carries an exact live descriptor allowlist. The Runtime therefore
        # never has to treat a missing allowlist as tenant-wide authority.
        readonly["capability_allowlist"] = [
            {
                "type": str(item["kind"]),
                "name": str(item["name"]),
                "id": str(item["id"]),
                "version": item.get("version"),
                "schema_hash": item.get("schema_hash"),
            }
            for item in [*allowed_tools, *allowed_mcp, *bridges]
        ]

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
        enable_dynamic_tools: bool = True,
    ) -> AgentTurn:
        assignment = await self._assignment(tenant_id, user_id, session_id)
        model = await self.model_service.get_model(tenant_id, model_id)
        if not model or not bool(model.get("is_enabled", True)):
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_MODEL_NOT_FOUND", status_code=400)
        provider_id = str(model.get("provider_id") or "")
        provider = await self.provider_service.get_provider(tenant_id, provider_id)
        if not provider or not bool(provider.get("is_enabled")):
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_PROVIDER_UNAVAILABLE", status_code=503)
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
        if not effective_reasoning_option and not effective_legacy_thinking and signed_thinking_mode:
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
                {"temperature": effective_temperature}
                if effective_temperature is not None
                else {}
            ),
            "pricing": {
                "input_price_per_1k": float(model.get("input_price_per_1k") or 0),
                "output_price_per_1k": float(model.get("output_price_per_1k") or 0),
            },
            "tools": {
                "enabled": bool(readonly.get("tools") or readonly.get("mcp")),
                "phase": "readonly" if readonly.get("items") or readonly.get("tools") or readonly.get("mcp") else "pure_text",
            },
            "readonly_capabilities": readonly,
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
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_LEASE_ISSUE_FAILED", status_code=503)
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
            },
        )
        if response.status_code >= 400:
            await self._fail_run(run_id, snapshot_id, "agent_turn_start_rejected")
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_TURN_START_FAILED", status_code=503)
        payload = response.json()
        returned_turn_id = str(((payload.get("turn") or {}).get("id")) or "")
        if returned_turn_id != str(run_id):
            await self._fail_run(run_id, snapshot_id, "agent_turn_identity_mismatch")
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_TURN_IDENTITY_MISMATCH", status_code=503)
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
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_EVENT_STREAM_FAILED", status_code=503)
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
            "GET", url, headers=headers,
            params={"after_sequence": max(0, int(after_sequence)), "limit": max(1, min(int(limit), 1000))},
        ) as response:
            if response.status_code >= 400:
                raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_EVENT_STREAM_FAILED", status_code=503)
            async for line in response.aiter_lines():
                if line:
                    frame.append(line)
                    continue
                if not frame:
                    continue
                data_raw = next((value[5:].strip() for value in frame if value.startswith("data:")), "")
                frame = []
                if not data_raw:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    envelope = json.loads(data_raw)
                    if not isinstance(envelope, dict):
                        continue
                    event_data = envelope.get("data")
                    if turn_id and (
                        not isinstance(event_data, dict)
                        or str(event_data.get("run_id") or "") != turn_id
                    ):
                        continue
                    event_type = str(envelope.get("event_type") or "")
                    yield envelope
                    if event_type in {"run_finished", "run_error"}:
                        terminal_status = str(
                            (event_data or {}).get("status") or "failed"
                        )
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
            raise AgentRuntimeControlError("AI_PLATFORM_AGENT_RUNTIME_APPROVAL_LOOKUP_FAILED", status_code=503)
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
        return payload if isinstance(payload, dict) else {"approval_id": approval_id, "status": "consumed"}

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
