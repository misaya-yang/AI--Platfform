"""Knowledge-base client contract + shared pure-data utilities.

- ``KnowledgeClientLike`` — structural Protocol for HTTP KB clients; use
  for type annotations in business code.
- ``ISLAMIC_SYNONYMS`` — pure data dictionary of Islamic-term synonyms
  used by assistant quality policies. Moved here so the assistant
  doesn't reach into ``src.services.knowledge`` for a static constant.
- ``get_authority_order`` / ``AUTHORITY_ORDER`` — pure function +
  backing dict for citation authority sorting. String-keyed for
  service-agnostic use.

These three items are the only ones the Phase-4 plan promoted from
``src.services.knowledge.*``; everything else in that module runs in
the KB service and is reached via HTTP (see KBProxyClient in gateway).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ._authority import AUTHORITY_ORDER, get_authority_order
from ._synonyms import ISLAMIC_SYNONYMS
from .utils import MULTIMODAL_EMBEDDING_MODELS, is_multimodal_embedding_model


@runtime_checkable
class KnowledgeClientLike(Protocol):
    """Minimal async contract for KB retrieval used by assistant tools."""

    async def search(
        self,
        query: str,
        *,
        dataset_id: str | None = None,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


__all__ = [
    "AUTHORITY_ORDER",
    "ISLAMIC_SYNONYMS",
    "MULTIMODAL_EMBEDDING_MODELS",
    "KnowledgeClientLike",
    "get_authority_order",
    "is_multimodal_embedding_model",
]
