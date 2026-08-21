"""Gateway-owned control plane for Codex Thread and Turn lifecycle."""

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
from ai_gateway_core.models import resolve_reasoning_option


class _Database(Protocol):
    async def fetchrow(self, query: str, *args): ...

    async def execute(self, query: str, *args): ...


@dataclass(frozen=True, slots=True)
class CandidateTurn:
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


class CodexRuntimeControlError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _provider_revision(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


class CodexRuntimeControlPlane:
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
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not runtime_url or not runtime_internal_token or not model_plane_base_url:
            raise ValueError("Codex Runtime control-plane endpoints and token are required")
        if not kernel_revision:
            raise ValueError("Codex Runtime kernel revision is required")
        self.database = database
        self.model_service = model_service
        self.provider_service = provider_service
        self.assignment_store = assignment_store
        self.lease_signer = lease_signer
        self.runtime_url = runtime_url.rstrip("/")
        self.runtime_internal_token = runtime_internal_token
        self.model_plane_base_url = model_plane_base_url.rstrip("/")
        self.kernel_revision = kernel_revision
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self._owns_http_client = http_client is None
        self.lease_ttl_seconds = max(
            30,
            min(int(os.getenv("CODEX_RUNTIME_LEASE_TTL_SECONDS", "900")), 3600),
        )
        self.max_model_calls = max(
            1,
            min(int(os.getenv("CODEX_RUNTIME_MAX_MODEL_CALLS_PER_TURN", "8")), 128),
        )
        self.max_cost_microusd = max(
            1,
            int(os.getenv("CODEX_RUNTIME_MAX_COST_MICROUSD_PER_TURN", "5000000")),
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
        if (
            assignment is None
            or assignment.runtime_owner != "codex_candidate"
            or assignment.kernel_revision != self.kernel_revision
        ):
            raise CodexRuntimeControlError("CODEX_RUNTIME_ASSIGNMENT_MISMATCH", status_code=403)
        return assignment

    async def _existing_thread(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        row = await self.database.fetchrow(
            """
            SELECT runtime_thread_id, last_sequence
              FROM assistant_runtime_threads
             WHERE tenant_id = $1 AND user_id = $2 AND session_id = $3
               AND deleted_at IS NULL
            """,
            tenant_id,
            user_id,
            session_id,
        )
        return dict(row) if row else None

    async def ensure_thread(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        existing = await self._existing_thread(tenant_id, user_id, session_id)
        if existing:
            return existing
        key = (tenant_id, user_id, session_id)
        lock = self._thread_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = await self._existing_thread(tenant_id, user_id, session_id)
            if existing:
                return existing
            start = {
                "model": model_id,
                "modelProvider": "ai-platform-gateway",
                "cwd": "/workspace",
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "config": {
                    "model_provider": "ai-platform-gateway",
                    "model": model_id,
                    "model_providers": {
                        "ai-platform-gateway": {
                            "name": "AI Platform Gateway Model Plane",
                            "base_url": self.model_plane_base_url,
                            "env_key": "CODEX_MODEL_PLANE_INTERNAL_TOKEN",
                            "wire_api": "responses",
                            "requires_openai_auth": False,
                            "supports_websockets": False,
                            "request_max_retries": 0,
                            "stream_max_retries": 0,
                        }
                    },
                },
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
                    return existing
                raise CodexRuntimeControlError(
                    "CODEX_RUNTIME_THREAD_CREATE_FAILED",
                    status_code=503,
                )
            payload = response.json()
            thread = payload.get("thread") if isinstance(payload, dict) else None
            thread_id = str((thread or {}).get("id") or "")
            if not thread_id:
                raise CodexRuntimeControlError(
                    "CODEX_RUNTIME_THREAD_CREATE_INVALID",
                    status_code=503,
                )
            return {"runtime_thread_id": uuid.UUID(thread_id), "last_sequence": 0}

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
    ) -> CandidateTurn:
        assignment = await self._assignment(tenant_id, user_id, session_id)
        model = await self.model_service.get_model(tenant_id, model_id)
        if not model or not bool(model.get("is_enabled", True)):
            raise CodexRuntimeControlError("CODEX_RUNTIME_MODEL_NOT_FOUND", status_code=400)
        provider_id = str(model.get("provider_id") or "")
        provider = await self.provider_service.get_provider(tenant_id, provider_id)
        if not provider or not bool(provider.get("is_enabled")):
            raise CodexRuntimeControlError("CODEX_RUNTIME_PROVIDER_UNAVAILABLE", status_code=503)
        thread = await self.ensure_thread(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            model_id=model_id,
        )
        profile = model.get("effective_capabilities")
        if not isinstance(profile, dict):
            raise CodexRuntimeControlError(
                "CODEX_RUNTIME_CAPABILITY_PROFILE_INVALID", status_code=503
            )
        requested = reasoning_option or legacy_thinking_level or "auto"
        resolved = resolve_reasoning_option(profile, requested)
        capability_revision = int(model.get("capability_revision") or 1)
        provider_revision = _provider_revision(provider.get("updated_at"))
        wire_protocols = profile.get("wire_protocols")
        if not isinstance(wire_protocols, dict):
            raise CodexRuntimeControlError(
                "CODEX_RUNTIME_WIRE_CAPABILITY_INVALID",
                status_code=503,
            )
        wire_protocol = str(wire_protocols.get("preferred") or "")
        supported_wires = wire_protocols.get("supported")
        if not isinstance(supported_wires, list) or wire_protocol not in supported_wires:
            raise CodexRuntimeControlError(
                "CODEX_RUNTIME_WIRE_CAPABILITY_INVALID",
                status_code=503,
            )
        output_limit = min(
            int(max_tokens or model.get("max_output_tokens") or 4096),
            int(model.get("max_output_tokens") or 4096),
        )
        output_limit = max(1, output_limit)
        runtime_thread_id = uuid.UUID(str(thread["runtime_thread_id"]))
        run_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()
        lease_id = uuid.uuid4()
        nonce_sha256 = sha256(secrets.token_bytes(32)).hexdigest()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        snapshot = {
            "schema_version": "codex-runtime-snapshot/v1",
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
            "reasoning": {
                "requested_option": resolved.requested,
                "effective_option": resolved.effective,
                "adapter_id": resolved.adapter_id,
                "canonical_effort": resolved.canonical_effort,
                "settings": resolved.settings,
                "fallback_reason": resolved.fallback_reason,
            },
            "limits": {
                "context_window": int(model.get("context_window") or 128000),
                "max_output_tokens": output_limit,
                "max_model_calls": self.max_model_calls,
            },
            "pricing": {
                "input_price_per_1k": float(model.get("input_price_per_1k") or 0),
                "output_price_per_1k": float(model.get("output_price_per_1k") or 0),
            },
            "tools": {"enabled": False, "phase": "pure_text"},
        }
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
                'codex-runtime-snapshot/v1', $9::jsonb, $10, $11, $12,
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
            raise CodexRuntimeControlError("CODEX_RUNTIME_LEASE_ISSUE_FAILED", status_code=503)
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
            },
        )
        if response.status_code >= 400:
            await self._fail_run(run_id, snapshot_id, "codex_turn_start_rejected")
            raise CodexRuntimeControlError("CODEX_RUNTIME_TURN_START_FAILED", status_code=503)
        payload = response.json()
        returned_turn_id = str(((payload.get("turn") or {}).get("id")) or "")
        if returned_turn_id != str(run_id):
            await self._fail_run(run_id, snapshot_id, "codex_turn_identity_mismatch")
            raise CodexRuntimeControlError("CODEX_RUNTIME_TURN_IDENTITY_MISMATCH", status_code=503)
        return CandidateTurn(
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
        turn: CandidateTurn,
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
                    "codex_event_stream_rejected",
                )
                raise CodexRuntimeControlError("CODEX_RUNTIME_EVENT_STREAM_FAILED", status_code=503)
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
                                        "kernel": "codex",
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
            UPDATE assistant_runs
               SET status = $2, usage = $3::jsonb, finished_at = NOW(), updated_at = NOW()
             WHERE run_id = $1 AND engine = 'codex_harness' AND status = 'running'
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
             WHERE run_id = $1 AND engine = 'codex_harness' AND status = 'running'
            """,
            run_id,
            reason,
        )


__all__ = [
    "CandidateTurn",
    "CodexRuntimeControlError",
    "CodexRuntimeControlPlane",
]
