"""Assistant-service backed LLM completion for eval judge runs."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.eval.evaluator_executor import LlmCompleteContext
from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)

ASSISTANT_CHAT_PATH = "/api/v1/assistant/chat"
DEFAULT_JUDGE_MODEL_ID = "qwen3.6-plus"
EVAL_WORKER_USER_ID = "eval-worker"


def _read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


@dataclass(frozen=True)
class EvalLlmSettings:
    enabled: bool = True
    assistant_base_url: str = "http://assistant-service:8093"
    default_judge_model_id: str = DEFAULT_JUDGE_MODEL_ID
    system_tenant_id: str = "default"
    timeout_read_s: float = 120.0


def load_eval_llm_settings() -> EvalLlmSettings:
    enabled = os.getenv("EVAL_LLM_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    return EvalLlmSettings(
        enabled=enabled,
        assistant_base_url=os.getenv("ASSISTANT_SERVICE_URL", "http://assistant-service:8093").rstrip("/"),
        default_judge_model_id=os.getenv("EVAL_JUDGE_MODEL_ID", DEFAULT_JUDGE_MODEL_ID).strip()
        or DEFAULT_JUDGE_MODEL_ID,
        system_tenant_id=os.getenv("EVAL_SYSTEM_TENANT_ID", "default").strip() or "default",
        timeout_read_s=_read_float_env("EVAL_LLM_TIMEOUT_READ_S", 120.0),
    )


def _build_assistant_signer() -> GatewaySecret | None:
    secret = os.getenv("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
    if not secret:
        return None
    return GatewaySecret(secret=secret)


class EvalAssistantLlmClient:
    """Call assistant-service /chat for bounded evaluator judge prompts."""

    def __init__(self, settings: EvalLlmSettings | None = None) -> None:
        self.settings = settings or load_eval_llm_settings()
        self._signer = _build_assistant_signer()

    async def complete(
        self,
        model_id: str,
        prompt: str,
        context: LlmCompleteContext,
    ) -> str:
        tenant_id = context.tenant_id or self.settings.system_tenant_id
        body = {
            "message": prompt,
            "model_id": model_id or self.settings.default_judge_model_id,
            "temperature": 0,
            "max_tokens": 512,
            "kb_mode": "off",
            "memory_mode": "off",
            "web_search_enabled": False,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers(tenant_id=tenant_id, body=encoded)

        timeout = httpx.Timeout(connect=5.0, read=self.settings.timeout_read_s, write=30.0, pool=10.0)
        async with httpx.AsyncClient(base_url=self.settings.assistant_base_url, timeout=timeout) as client:
            response = await client.post(ASSISTANT_CHAT_PATH, headers=headers, content=encoded)

        if response.status_code >= 400:
            raise RuntimeError(f"assistant-service judge chat failed with HTTP {response.status_code}")

        payload = response.json()
        content = str(payload.get("content") or "").strip()
        if not content:
            raise RuntimeError("assistant-service judge chat returned empty content")
        return content

    def _headers(self, *, tenant_id: str, body: bytes) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-User-Id": EVAL_WORKER_USER_ID,
            "X-Tenant-Id": tenant_id,
            "X-User-Tier": "normal",
            "X-User-Type": "system",
            "X-User-Roles": "admin",
        }
        if self._signer is not None:
            headers[self._signer.header_name] = self._signer.sign(
                method="POST",
                path=ASSISTANT_CHAT_PATH,
                query="",
                body=body,
            )
        return headers


def build_eval_llm_complete(
    settings: EvalLlmSettings | None = None,
) -> Callable[..., Awaitable[str]] | None:
    """Factory for EvaluatorExecutor.llm_complete."""
    resolved = settings or load_eval_llm_settings()
    if not resolved.enabled:
        logger.info("Eval LLM judge disabled via EVAL_LLM_ENABLED")
        return None

    client = EvalAssistantLlmClient(resolved)

    async def _complete(model_id: str, prompt: str, context: LlmCompleteContext | None = None) -> str:
        ctx = context or LlmCompleteContext(tenant_id=resolved.system_tenant_id)
        judge_model = model_id or resolved.default_judge_model_id
        return await client.complete(judge_model, prompt, ctx)

    return _complete
