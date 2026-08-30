"""V2 Agent Runtime client used by live Agent eval runs.

The eval candidate must exercise the same public Thread/Turn/Item boundary as
the product. Calling a parallel model/tool loop here would make the
quality gate prove the old Python loop instead of the Runtime that is being
rolled out.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from ai_gateway_core.eval.runtime_contract import assert_runtime_observation

from .assistant_trace_capture import build_assistant_runtime_trace

V2_THREADS_PATH = "/api/v2/agent/threads"
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
    trace_payload: dict[str, Any] | None = None


class EvalCandidateClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "AGENT_EVAL_GATEWAY_URL",
            os.getenv("GATEWAY_URL", "http://gateway:8080"),
        ).rstrip("/")
        self.token = (
            os.getenv("AGENT_EVAL_AUTH_TOKEN", "").strip()
            or os.getenv("GATEWAY_TOKEN", "").strip()
            or os.getenv("GATEWAY_ADMIN_JWT", "").strip()
        )
        self.api_key = os.getenv("AGENT_EVAL_API_KEY", "").strip() or os.getenv(
            "GATEWAY_API_KEY", ""
        ).strip()

    async def run(
        self,
        *,
        tenant_id: str,
        run_case_id: str,
        message: str,
        config: dict[str, Any],
        on_run_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> EvalCandidateResult:
        if not self.token and not self.api_key:
            raise RuntimeError(
                "AGENT_EVAL_AUTH_TOKEN/GATEWAY_TOKEN/GATEWAY_ADMIN_JWT or "
                "AGENT_EVAL_API_KEY/GATEWAY_API_KEY is required for V2 live eval"
            )
        if config.get("system_prompt_override"):
            raise RuntimeError(
                "V2 Agent Runtime does not support eval system_prompt_override; "
                "refusing to evaluate a different prompt"
            )
        configured_model_id = str(config.get("model_id") or "").strip()
        model_id = configured_model_id if configured_model_id not in {"", "current"} else None
        headers = self._auth_headers()
        headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                # The token remains the authority; these headers make the
                # eval scope explicit for request tracing and test servers.
                "X-Tenant-Id": tenant_id,
                "X-User-Id": EVAL_CANDIDATE_USER_ID,
            }
        )
        thread_body: dict[str, Any] = {"session_id": run_case_id}
        if model_id is not None:
            thread_body["model_id"] = model_id

        trace_id = ""
        output_parts: list[str] = []
        usage: dict[str, Any] = {}
        fingerprint: dict[str, Any] = {}
        terminal_error: str | None = None
        terminal_events: list[str] = []
        observed_events: list[dict[str, Any]] = []
        event_counts: dict[str, int] = {}
        started_at = time.time()
        first_token_at: float | None = None
        terminal_status = "failed"
        timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=False,
        ) as client:
            thread_response = await client.post(
                V2_THREADS_PATH, headers=headers, json=thread_body
            )
            if thread_response.status_code >= 400:
                raise RuntimeError(
                    f"Agent Runtime thread create failed with HTTP {thread_response.status_code}"
                )
            thread_payload = thread_response.json()
            thread = thread_payload.get("thread") if isinstance(thread_payload, dict) else None
            runtime = thread.get("runtime") if isinstance(thread, dict) else None
            if not isinstance(runtime, dict) or runtime.get("owner") != "agent_runtime":
                raise RuntimeError("Agent Eval candidate is not owned by agent_runtime")
            thread_id = str((thread or {}).get("thread_id") or (thread or {}).get("id") or "")
            if not thread_id:
                raise RuntimeError("Agent Runtime thread response has no thread_id")

            turn_body = {
                "message": message,
                "model_id": model_id,
                "reasoning_option": config.get("reasoning_option"),
                "thinking_level": config.get("thinking_level"),
                "temperature": config.get("temperature"),
                "max_tokens": config.get("max_tokens"),
                "kb_dataset_ids": config.get("kb_dataset_ids") or [],
                "kb_mode": config.get("kb_mode") or "off",
                "kb_top_k": config.get("kb_top_k") or 5,
                "web_search_enabled": bool(config.get("web_search_enabled", False)),
                "web_search_max_results": config.get("web_search_max_results") or 5,
            }
            turn_body = {key: value for key, value in turn_body.items() if value is not None}
            turn_response = await client.post(
                f"{V2_THREADS_PATH}/{thread_id}/turns", headers=headers, json=turn_body
            )
            if turn_response.status_code >= 400:
                raise RuntimeError(
                    f"Agent Runtime turn create failed with HTTP {turn_response.status_code}"
                )
            turn_payload = turn_response.json()
            turn = turn_payload.get("turn") if isinstance(turn_payload, dict) else None
            turn_id = str((turn or {}).get("id") or "")
            if not turn_id:
                raise RuntimeError("Agent Runtime turn response has no turn_id")
            events_path = str((turn or {}).get("events_url") or "")
            if not events_path:
                raise RuntimeError("Agent Runtime turn response has no events_url")
            if events_path.startswith("http"):
                events_path = events_path.split(self.base_url, 1)[-1]
            async with client.stream("GET", events_path, headers={**headers, "Accept": "text/event-stream"}) as response:
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"Agent Runtime event stream failed with HTTP {response.status_code}"
                    )
                frame: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        frame.append(line)
                        continue
                    if not frame:
                        continue
                    raw_line = next((value[5:].strip() for value in frame if value.startswith("data:")), "")
                    frame = []
                    if not raw_line:
                        continue
                    try:
                        envelope = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event = envelope.get("event") if isinstance(envelope, dict) else None
                    if isinstance(envelope, dict):
                        observed_events.append(envelope)
                    raw = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(raw, dict):
                        continue
                    event_type = str(raw.get("event_type") or "")
                    data = raw.get("data")
                    if event_type:
                        event_counts[event_type] = event_counts.get(event_type, 0) + 1
                    if event_type == "run_started" and isinstance(data, dict):
                        trace_id = str(data.get("run_id") or trace_id or turn_id)
                        if data.get("kernel_revision") is not None:
                            fingerprint["runtime_revision"] = data["kernel_revision"]
                        if trace_id and on_run_started is not None:
                            await on_run_started(trace_id)
                    elif event_type == "context_budget" and isinstance(data, dict):
                        fingerprint = candidate_fingerprint_from_context(data)
                    elif event_type == "text_delta":
                        if first_token_at is None:
                            first_token_at = time.time()
                        if isinstance(data, str):
                            output_parts.append(data)
                        elif isinstance(data, dict):
                            output_parts.append(str(data.get("delta") or data.get("content") or ""))
                    elif event_type == "usage" and isinstance(data, dict):
                        usage = dict(data)
                    elif event_type in {"run_finished", "run_error", "cancelled"}:
                        terminal_events.append(event_type)
                        raw_status = (
                            str(data.get("status") or "").lower()
                            if isinstance(data, dict)
                            else ""
                        )
                        terminal_status = (
                            "succeeded"
                            if event_type == "run_finished"
                            and raw_status in {"", "completed", "succeeded"}
                            else "cancelled"
                            if event_type == "cancelled" or raw_status == "cancelled"
                            else "failed"
                        )
                        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
                            usage = dict(data["usage"])
                    if event_type in {"error", "run_error", "cancelled"}:
                        terminal_error = (
                            str(data.get("message") or data.get("error") or "")
                            if isinstance(data, dict)
                            else str(data or "")
                        )
        if len(terminal_events) != 1:
            raise RuntimeError(
                "Agent Runtime event stream must contain exactly one terminal "
                "event (run_finished, run_error, or cancelled); "
                f"observed {terminal_events or 'none'}"
            )
        try:
            assert_runtime_observation(thread, observed_events)
        except ValueError as exc:
            raise RuntimeError(f"Agent Runtime observation contract failed: {exc}") from exc
        if not trace_id:
            raise RuntimeError(terminal_error or "Agent Runtime event stream returned no run_id")
        trace_payload: dict[str, Any] | None = None
        try:
            trace_payload = build_assistant_runtime_trace(
                run_id=trace_id,
                request_id=run_case_id,
                tenant_id=tenant_id,
                user_id=EVAL_CANDIDATE_USER_ID,
                session_id=run_case_id,
                message=message,
                snapshot={
                    "model": {
                        "id": model_id or fingerprint.get("model_id"),
                        "provider": fingerprint.get("provider"),
                    },
                    "publication": {"channel": "builtin"},
                },
                status=terminal_status,
                started_at=started_at,
                ended_at=time.time(),
                first_token_latency_ms=(
                    int((first_token_at - started_at) * 1000) if first_token_at else 0
                ),
                output="".join(output_parts),
                event_counts=event_counts,
                usage=usage,
                error_type="runtime_error" if terminal_error else None,
            )
        except (TypeError, ValueError):
            # Public Runtime turn ids are UUIDs. Tests and third-party mock
            # servers may use placeholders; keep the protocol result usable
            # while refusing to persist a malformed trace identity.
            trace_payload = None
        return EvalCandidateResult(
            trace_id=trace_id,
            output="".join(output_parts),
            usage=usage,
            fingerprint=fingerprint,
            error=terminal_error or None,
            trace_payload=trace_payload,
        )

    def _auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.api_key:
            return {"X-API-Key": self.api_key}
        return {}
