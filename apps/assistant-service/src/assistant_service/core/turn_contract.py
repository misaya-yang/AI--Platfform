"""Additive Assistant run/session/turn contract helpers."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

TURN_CONTRACT_SCHEMA_VERSION = "assistant-turn-contract/v1"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str | int | float | bool):
        return enum_value
    return str(value)


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_context_snapshot(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    mode: str,
    model_id: str,
    provider: Any = None,
    trace_id: str | None = None,
    otel_trace_id: str | None = None,
    policy: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
    workspace: dict[str, Any] | None = None,
    tools: dict[str, Any] | None = None,
    bootstrap: dict[str, Any] | None = None,
    surface: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, prompt-free snapshot of context compiler inputs."""

    payload: dict[str, Any] = {
        "schema_version": TURN_CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "thread_id": session_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "mode": mode,
        "model_id": model_id,
        "provider": _json_safe(provider),
        "trace_id": trace_id,
        "otel_trace_id": otel_trace_id,
        "policy": _json_safe(policy or {}),
        "memory": _json_safe(memory or {}),
        "workspace": _json_safe(workspace or {}),
        "tools": _json_safe(tools or {}),
        "bootstrap": _json_safe(bootstrap or {}),
        "surface": _json_safe(surface or {}),
    }
    snapshot_hash = _stable_hash(payload)
    payload["snapshot_hash"] = snapshot_hash
    payload["snapshot_id"] = f"ctx_{snapshot_hash}"
    return payload


def build_terminal_envelope(
    *,
    run_id: str,
    request_id: str,
    session_id: str,
    tenant_id: str,
    user_id: str,
    mode: str,
    status: str,
    exit_reason: str,
    started_at: float,
    model_id: str,
    provider: Any = None,
    ended_at: float | None = None,
    trace_id: str | None = None,
    otel_trace_id: str | None = None,
    checkpoint_id: str | None = None,
    context_snapshot: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: Any = None,
    resume_ready: bool = False,
    approval_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Build the terminal turn envelope shared by stream/non-stream paths."""

    finished_at = ended_at or time.time()
    payload: dict[str, Any] = {
        "schema_version": TURN_CONTRACT_SCHEMA_VERSION,
        "run_id": run_id,
        "request_id": request_id,
        "thread_id": session_id,
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "mode": mode,
        "status": status,
        "exit_reason": exit_reason,
        "started_at": started_at,
        "ended_at": finished_at,
        "duration_ms": max(0, int((finished_at - started_at) * 1000)),
        "model_id": model_id,
        "provider": _json_safe(provider),
        "trace_id": trace_id,
        "otel_trace_id": otel_trace_id,
        "checkpoint_id": checkpoint_id,
        "context_snapshot_id": (context_snapshot or {}).get("snapshot_id"),
        "context_snapshot": _json_safe(context_snapshot or {}),
        "usage": _json_safe(usage or {}),
        "resume_ready": bool(resume_ready),
        "approval_id": approval_id,
        "task_id": task_id,
    }
    if error:
        payload["error"] = str(error)[:500]
    return payload
