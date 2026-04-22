"""Knowledge-base client contract.

The assistant never runs KB retrieval in-process; it calls the KB service
over HTTP. This Protocol describes the HTTP-client shape so downstream
tests and fakes can substitute without importing the concrete
``KBProxyClient``. The concrete client itself is a Phase-3 placeholder
and may be promoted to live in this package if the gateway also uses it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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


__all__ = ["KnowledgeClientLike"]
