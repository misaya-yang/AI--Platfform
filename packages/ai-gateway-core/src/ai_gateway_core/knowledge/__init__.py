"""Knowledge-base client contract and shared utilities.

- ``KnowledgeClientLike`` — structural Protocol for HTTP KB clients; use
  for type annotations in business code.
- ``KBProxyClient`` — HTTP client for the knowledge service.
- ``is_multimodal_embedding_model`` — static multimodal model predicate.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .proxy_client import KB_SERVICE_URL, KBProxyClient, ProxyRetrieveResult
from .utils import MULTIMODAL_EMBEDDING_MODELS, is_multimodal_embedding_model


@runtime_checkable
class KnowledgeClientLike(Protocol):
    """Minimal async contract for KB retrieval used by assistant tools."""

    async def list_datasets(self, user: Any) -> list[dict[str, Any]]: ...

    async def retrieve(
        self,
        user: Any,
        dataset_id: str,
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
        score_threshold: float = 0.0,
        **kwargs: Any,
    ) -> tuple[list[Any], dict[str, Any]]: ...


__all__ = [
    "KB_SERVICE_URL",
    "KBProxyClient",
    "MULTIMODAL_EMBEDDING_MODELS",
    "KnowledgeClientLike",
    "ProxyRetrieveResult",
    "is_multimodal_embedding_model",
]
