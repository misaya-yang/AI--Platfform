"""Gateway client for knowledge-service KB RAGAS evaluation."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

import httpx
from ai_gateway_core.auth.gateway_secret import GatewaySecret
from ai_gateway_core.comm.client import (
    InternalServiceClient,
    InternalServiceClientConfig,
    InternalServiceHTTPError,
)
from ai_gateway_core.knowledge import KB_SERVICE_URL
from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)

_gateway_secret_signer: GatewaySecret | None = None
_signer_lock = threading.Lock()


def _get_signer() -> GatewaySecret | None:
    global _gateway_secret_signer
    if _gateway_secret_signer is not None:
        return _gateway_secret_signer
    with _signer_lock:
        if _gateway_secret_signer is not None:
            return _gateway_secret_signer
        secret = (
            os.environ.get("GATEWAY_KNOWLEDGE_SHARED_SECRET", "").strip()
            or os.environ.get("GATEWAY_ASSISTANT_SHARED_SECRET", "").strip()
        )
        if not secret:
            return None
        _gateway_secret_signer = GatewaySecret(secret=secret)
        return _gateway_secret_signer


@dataclass(frozen=True)
class KbRagasMetricResult:
    metric: str
    score: float
    explanation: str
    label: str
    judge_model: str | None = None


class KbRagasClient:
    def __init__(self, *, base_url: str | None = None, timeout_s: float = 120.0) -> None:
        self.base_url = (base_url or os.getenv("KB_SERVICE_URL", KB_SERVICE_URL)).rstrip("/")
        self.timeout = httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=10.0)
        self._service_client: InternalServiceClient | None = None

    def _get_service_client(self) -> InternalServiceClient:
        if self._service_client is None:
            self._service_client = InternalServiceClient(
                InternalServiceClientConfig(
                    name="knowledge-service",
                    base_url=self.base_url,
                    timeout=self.timeout,
                    gateway_secret=_get_signer(),
                )
            )
        return self._service_client

    async def close(self) -> None:
        if self._service_client:
            await self._service_client.close()

    async def evaluate_retrieval(
        self,
        *,
        query: str,
        contexts: list[str],
        metrics: list[str] | None = None,
        ground_truth: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> list[KbRagasMetricResult]:
        payload = {
            "query": query,
            "contexts": contexts,
            "metrics": metrics,
            "ground_truth": ground_truth,
            "llm_config": llm_config,
        }
        try:
            body = await self._get_service_client().request_json(
                "POST",
                "/api/v1/internal/eval/ragas",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except InternalServiceHTTPError as exc:
            raise RuntimeError(
                f"knowledge-service RAGAS eval failed with HTTP {exc.status_code}"
            ) from exc

        judge_model = str(body.get("judge_model") or "")
        results: list[KbRagasMetricResult] = []
        for item in body.get("results") or []:
            if not isinstance(item, dict):
                continue
            results.append(
                KbRagasMetricResult(
                    metric=str(item.get("metric") or ""),
                    score=float(item.get("score") or 0.0),
                    explanation=str(item.get("explanation") or ""),
                    label=str(item.get("label") or "review"),
                    judge_model=judge_model or None,
                )
            )
        return results


def build_kb_ragas_complete(client: KbRagasClient | None = None) -> KbRagasClient:
    return client or KbRagasClient()
