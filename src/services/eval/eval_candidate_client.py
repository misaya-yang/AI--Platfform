"""Internal streaming client used by live Agent eval runs."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from ai_gateway_core.auth.gateway_secret import GatewaySecret

ASSISTANT_STREAM_PATH = "/api/v1/assistant/chat/stream"
EVAL_CANDIDATE_USER_ID = "eval-candidate"


def candidate_fingerprint_from_context(data: dict[str, Any]) -> dict[str, Any]:
    fingerprint = {}
    system_prompt_hash = data.get("candidate_system_prompt_hash") or data.get(
        "system_prompt_hash"
    )
    if system_prompt_hash is not None:
        fingerprint["system_prompt_hash"] = system_prompt_hash
    if data.get("runtime_revision") is not None:
        fingerprint["runtime_revision"] = data["runtime_revision"]
    tool_schema_hash = data.get("available_tool_schema_hash") or data.get(
        "tool_schema_hash"
    )
    if tool_schema_hash is not None:
        fingerprint["tool_schema_hash"] = tool_schema_hash
    snapshot = data.get("context_snapshot")
    if not isinstance(snapshot, dict):
        return fingerprint
    policy = snapshot.get("policy") if isinstance(snapshot.get("policy"), dict) else {}
    bootstrap = snapshot.get("bootstrap") if isinstance(snapshot.get("bootstrap"), dict) else {}
    fingerprint.update(
        {
            "model_id": snapshot.get("model_id"),
            "provider": snapshot.get("provider"),
            "sampling": {
                "temperature": bootstrap.get("temperature"),
                "max_tokens": bootstrap.get("max_tokens"),
            },
            "execution_policy": {
                key: policy.get(key)
                for key in (
                    "execution_profile",
                    "runtime_mode",
                    "kb_mode",
                    "web_search_enabled",
                )
            },
            "rag_config_hash": policy.get("rag_config_hash"),
            "rag_revision_hash": policy.get("rag_revision_hash"),
        }
    )
    return fingerprint


@dataclass(frozen=True)
class EvalCandidateResult:
    trace_id: str
    output: str
    usage: dict[str, Any] = field(default_factory=dict)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class EvalCandidateClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("ASSISTANT_SERVICE_URL", "http://assistant-service:8093").rstrip(
            "/"
        )
        secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
        self.signer = GatewaySecret(secret=secret) if secret else None

    async def run(
        self,
        *,
        tenant_id: str,
        run_case_id: str,
        message: str,
        config: dict[str, Any],
        on_run_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> EvalCandidateResult:
        if self.signer is None:
            raise RuntimeError("GATEWAY_ASSISTANT_SHARED_SECRET is required for live eval")
        model_id = str(config.get("model_id") or "").strip()
        if not model_id or model_id == "current":
            model_id = "qwen3.7-plus"
        body = {
            "message": message,
            "session_id": run_case_id,
            "history": [],
            "model_id": model_id,
            "temperature": config.get("temperature", 0.7),
            "max_tokens": config.get("max_tokens"),
            "kb_dataset_ids": config.get("kb_dataset_ids") or [],
            "kb_mode": config.get("kb_mode") or "auto",
            "kb_top_k": config.get("kb_top_k") or 5,
            "web_search_enabled": bool(config.get("web_search_enabled", False)),
            "web_search_max_results": config.get("web_search_max_results") or 5,
            "execution_profile": config.get("execution_profile") or "safe",
            "memory_mode": "off",
            "memory_profile": "off",
            "runtime_mode": config.get("runtime_mode") or "compat",
            "context_detail": True,
            "eval_run": True,
            "eval_system_prompt_override": config.get("system_prompt_override"),
        }
        body = {key: value for key, value in body.items() if value is not None}
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-User-Id": EVAL_CANDIDATE_USER_ID,
            "X-Tenant-Id": tenant_id,
            "X-User-Tier": "normal",
            "X-User-Type": "system",
            "X-User-Roles": "admin",
        }
        headers[self.signer.header_name] = self.signer.sign(
            method="POST",
            path=ASSISTANT_STREAM_PATH,
            query="",
            body=encoded,
        )

        trace_id = ""
        output_parts: list[str] = []
        usage: dict[str, Any] = {}
        fingerprint: dict[str, Any] = {}
        terminal_error: str | None = None
        timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=10.0)
        async with (
            httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client,
            client.stream(
                "POST", ASSISTANT_STREAM_PATH, headers=headers, content=encoded
            ) as response,
        ):
            if response.status_code >= 400:
                raise RuntimeError(
                    f"assistant-service candidate stream failed with HTTP {response.status_code}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("event_type") or "")
                data = event.get("data")
                if event_type == "run_started" and isinstance(data, dict):
                    trace_id = str(data.get("run_id") or trace_id)
                    if trace_id and on_run_started is not None:
                        await on_run_started(trace_id)
                elif event_type == "context_budget" and isinstance(data, dict):
                    fingerprint = candidate_fingerprint_from_context(data)
                elif event_type == "text_delta":
                    if isinstance(data, str):
                        output_parts.append(data)
                    elif isinstance(data, dict):
                        output_parts.append(str(data.get("delta") or data.get("content") or ""))
                elif event_type == "usage" and isinstance(data, dict):
                    usage = dict(data)
                elif event_type in {"done", "run_completed", "run_stopped", "run_finished"}:
                    if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                        usage = dict(data["usage"])
                elif event_type in {"error", "run_error"}:
                    terminal_error = (
                        str(data.get("message") or data.get("error") or "")
                        if isinstance(data, dict)
                        else str(data or "")
                    )
        if not trace_id:
            raise RuntimeError(terminal_error or "assistant candidate stream returned no run_id")
        return EvalCandidateResult(
            trace_id=trace_id,
            output="".join(output_parts),
            usage=usage,
            fingerprint=fingerprint,
            error=terminal_error or None,
        )
