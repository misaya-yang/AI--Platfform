"""Gateway client for Assistant-owned Agent runtime-memory cleanup."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from ai_gateway_core.agents import (
    validate_runtime_cleanup_inventory,
    validate_runtime_cleanup_plan,
    validate_runtime_cleanup_receipt,
)
from ai_gateway_core.auth.gateway_secret import GatewaySecret

_INVENTORY_PATH = "/api/v1/assistant/internal/runtime-memory-cleanup/inventory"
_EXECUTE_PATH = "/api/v1/assistant/internal/runtime-memory-cleanup/execute"


class AgentRuntimeCleanupClientError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AgentRuntimeCleanupClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("ASSISTANT_SERVICE_URL", "http://assistant-service:8093").rstrip(
            "/"
        )
        secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
        # Governance deletion always binds the canonical body/path to the HMAC.
        # The shared verifier accepts v2 regardless of the deployment's default
        # signing version, preserving rotation/replay behavior.
        self.signer = GatewaySecret(secret=secret, version="v2") if secret else None

    @staticmethod
    def _encode(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    async def _post(
        self,
        *,
        path: str,
        tenant_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.signer is None:
            raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_AUTH_UNAVAILABLE")
        encoded = self._encode(payload)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-User-Id": "agent-governance-cleanup",
            "X-Tenant-Id": tenant_id,
            "X-User-Tier": "normal",
            "X-User-Type": "system",
            "X-User-Roles": "admin",
        }
        headers[self.signer.header_name] = self.signer.sign(
            method="POST",
            path=path,
            query="",
            body=encoded,
        )
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
            ) as client:
                response = await client.post(path, headers=headers, content=encoded)
            if response.status_code >= 400:
                raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_UPSTREAM_REJECTED")
            value = response.json()
        except AgentRuntimeCleanupClientError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise AgentRuntimeCleanupClientError(
                "AGENT_RUNTIME_CLEANUP_UPSTREAM_UNAVAILABLE"
            ) from exc
        if not isinstance(value, dict):
            raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_RECEIPT_INVALID")
        return value

    async def inspect(self, plan_value: object) -> dict[str, Any]:
        plan = validate_runtime_cleanup_plan(plan_value)
        value = await self._post(
            path=_INVENTORY_PATH,
            tenant_id=plan["tenant_id"],
            payload={"plan": plan},
        )
        try:
            return validate_runtime_cleanup_inventory(value, plan=plan)
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_INVENTORY_INVALID") from exc

    async def execute(
        self,
        *,
        plan_value: object,
        inventory_value: object,
    ) -> dict[str, Any]:
        plan = validate_runtime_cleanup_plan(plan_value)
        inventory = validate_runtime_cleanup_inventory(
            inventory_value,
            plan=plan,
        )
        value = await self._post(
            path=_EXECUTE_PATH,
            tenant_id=plan["tenant_id"],
            payload={"plan": plan, "inventory": inventory},
        )
        try:
            return validate_runtime_cleanup_receipt(
                value,
                plan=plan,
                inventory=inventory,
            )
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_RECEIPT_INVALID") from exc


__all__ = ["AgentRuntimeCleanupClient", "AgentRuntimeCleanupClientError"]
