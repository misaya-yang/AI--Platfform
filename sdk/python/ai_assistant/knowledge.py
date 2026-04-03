"""
Knowledge-base module — dataset listing and RAG-powered Q&A.

Wraps ``/api/v1/assistant/datasets`` and delegates KB-aware chat to the
``ChatModule`` with appropriate ``kb_*`` parameters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ai_assistant.models.events import StreamEvent
from ai_assistant.models.response import ChatResponse, Dataset
from ai_assistant.transport.http import HTTPTransport

if TYPE_CHECKING:
    from ai_assistant.chat import ChatModule


class KnowledgeModule:
    """High-level knowledge-base operations."""

    _DATASETS_PATH = "/api/v1/assistant/datasets"

    def __init__(self, transport: HTTPTransport, chat: ChatModule) -> None:
        self._transport = transport
        self._chat = chat

    async def list_datasets(self) -> list[Dataset]:
        """List all knowledge-base datasets accessible to the current user."""
        resp = await self._transport.request("GET", self._DATASETS_PATH)
        data = resp.json()
        datasets_raw = data.get("datasets", data) if isinstance(data, dict) else data
        return [Dataset.from_dict(d) for d in datasets_raw]

    async def ask(
        self,
        question: str,
        dataset_ids: list[str],
        *,
        model_id: str = "gemini-3-flash-preview",
        kb_mode: str = "auto",
        kb_top_k: int | None = None,
        kb_score_threshold: float | None = None,
        kb_include_images: bool | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Ask a question using knowledge-base retrieval (non-streaming).

        This is a convenience wrapper around ``ChatModule.send`` with the
        KB parameters pre-filled.
        """
        return await self._chat.send(
            question,
            session_id=session_id,
            model_id=model_id,
            kb_dataset_ids=dataset_ids,
            kb_mode=kb_mode,
            kb_top_k=kb_top_k,
            kb_score_threshold=kb_score_threshold,
            kb_include_images=kb_include_images,
            **kwargs,
        )

    async def ask_stream(
        self,
        question: str,
        dataset_ids: list[str],
        *,
        model_id: str = "gemini-3-flash-preview",
        kb_mode: str = "auto",
        kb_top_k: int | None = None,
        kb_score_threshold: float | None = None,
        kb_include_images: bool | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Ask a question using knowledge-base retrieval (streaming).

        Yields ``StreamEvent`` objects including ``context_retrieved`` events
        containing the retrieved chunks.
        """
        async for event in self._chat.stream(
            question,
            session_id=session_id,
            model_id=model_id,
            kb_dataset_ids=dataset_ids,
            kb_mode=kb_mode,
            kb_top_k=kb_top_k,
            kb_score_threshold=kb_score_threshold,
            kb_include_images=kb_include_images,
            **kwargs,
        ):
            yield event
