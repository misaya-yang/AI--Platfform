"""
KB Proxy Client — Lightweight HTTP client for calling the KB microservice.

Used by assistant/quiz when KnowledgeService is not available locally
(production mode: KB runs as separate microservice at :8092).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

from ai_gateway_core.auth.gateway_secret import GatewaySecret

logger = logging.getLogger(__name__)

KB_SERVICE_URL = os.getenv("KB_SERVICE_URL", "http://knowledge-service:8092")
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)

# Lazy singleton — constructed the first time a request is signed so
# deployments without the env set fail loud on first call (and let
# dev envs start without the secret). The ``threading.Lock`` guards
# against two concurrent first-requests each constructing their own
# ``GatewaySecret`` instance; not a correctness issue (both instances
# produce valid signatures) but wasteful and harder to reason about.
_gateway_secret_signer: GatewaySecret | None = None
_signer_lock = threading.Lock()


def _get_signer() -> GatewaySecret | None:
    """Return a signer if ``GATEWAY_ASSISTANT_SHARED_SECRET`` is set.

    Returns ``None`` in dev environments without the secret configured;
    callers then skip the HMAC header and rely on
    ``KNOWLEDGE_APP__ALLOW_ANONYMOUS=true`` on the KB side. In prod the
    secret is always set, so the signer is always constructed.
    """
    global _gateway_secret_signer
    if _gateway_secret_signer is not None:
        return _gateway_secret_signer
    with _signer_lock:
        # Re-check inside the lock — standard double-checked locking.
        if _gateway_secret_signer is not None:
            return _gateway_secret_signer
        secret = os.environ.get("GATEWAY_ASSISTANT_SHARED_SECRET")
        if not secret:
            return None
        _gateway_secret_signer = GatewaySecret(secret=secret)
        return _gateway_secret_signer


@dataclass
class ProxyRetrieveResult:
    """Mimics the local RetrieveResult interface for compatibility."""
    text: str
    score: float
    segment_id: str | None = None
    document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    image_url: str | None = None


class KBProxyClient:
    """HTTP client to call KB microservice retrieve API.

    Drop-in replacement for KnowledgeService.retrieve() and list_datasets()
    when running in microservice mode.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or KB_SERVICE_URL).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=_TIMEOUT,
                transport=httpx.AsyncHTTPTransport(retries=1),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _user_headers(self, user: Any) -> dict[str, str]:
        """Build headers to pass user context to KB microservice.

        Includes an HMAC ``X-Gateway-Secret`` header (signed with
        ``GATEWAY_ASSISTANT_SHARED_SECRET``) so the KB service's
        ``GatewaySecretAuthMiddleware`` accepts the request. Without
        the header KB returns 401 once the middleware is active
        (K5c prod deploy, 2026-04-24)."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = getattr(user, "user_id", "") or ""
            headers["X-Tenant-Id"] = getattr(user, "tenant_id", "") or ""
            headers["X-User-Tier"] = getattr(user, "tier", "") or ""
        signer = _get_signer()
        if signer is not None:
            headers["X-Gateway-Secret"] = signer.sign()
        return headers

    async def health_check(self) -> bool:
        """Check if KB service is reachable."""
        try:
            resp = await self._get_client().get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    async def list_datasets(self, user: Any) -> list[dict[str, Any]]:
        """List datasets from KB microservice."""
        try:
            resp = await self._get_client().get(
                "/api/v1/knowledge/datasets",
                headers=self._user_headers(user),
            )
            if resp.status_code != 200:
                logger.warning(f"KB list_datasets failed: {resp.status_code}")
                return []
            data = resp.json()
            # Handle both {datasets: [...]} and direct array
            if isinstance(data, list):
                return data
            return data.get("datasets", data.get("data", []))
        except Exception as e:
            logger.warning(f"KB list_datasets error: {e}")
            return []

    async def retrieve(
        self,
        user: Any,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
        score_threshold: float = 0.0,
        **kwargs: Any,
    ) -> tuple[list[ProxyRetrieveResult], dict[str, Any]]:
        """Retrieve chunks from KB microservice.

        Returns (results, metadata) matching KnowledgeService.retrieve() signature.
        """
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "mode": mode,
        }
        if score_threshold > 0:
            payload["score_threshold"] = score_threshold

        try:
            resp = await self._get_client().post(
                f"/api/v1/knowledge/{dataset_id}/retrieve",
                json=payload,
                headers=self._user_headers(user),
            )
            if resp.status_code != 200:
                logger.warning(f"KB retrieve failed for {dataset_id}: {resp.status_code}")
                return [], {"error": f"HTTP {resp.status_code}"}

            data = resp.json()
            results_raw = data.get("results", [])
            meta = data.get("metadata", {})

            results = []
            for r in results_raw:
                results.append(ProxyRetrieveResult(
                    text=r.get("text", ""),
                    score=float(r.get("score", 0.0)),
                    segment_id=r.get("segment_id"),
                    document_id=r.get("document_id"),
                    metadata=r.get("metadata", {}),
                    image_url=r.get("image_url"),
                ))

            return results, meta

        except Exception as e:
            logger.warning(f"KB retrieve error for {dataset_id}: {e}")
            return [], {"error": str(e)}

    async def require_dataset_access(self, user: Any, dataset_id: str, required: str = "viewer") -> dict:
        """Check dataset access — delegates to KB service."""
        # In proxy mode, the KB service handles auth via X-User-Id/X-Tenant-Id headers
        # Just verify the dataset exists
        try:
            resp = await self._get_client().get(
                f"/api/v1/knowledge/datasets/{dataset_id}",
                headers=self._user_headers(user),
            )
            if resp.status_code == 200:
                return resp.json()
            return {}
        except Exception:
            return {}
