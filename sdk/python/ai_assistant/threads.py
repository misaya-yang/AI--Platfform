"""Native V2 Thread/Turn/Item SDK module (V1 remains unchanged)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ai_assistant.models.events import StreamEvent
    from ai_assistant.transport.http import HTTPTransport


class ThreadModule:
    _BASE = "/api/v2/agent/threads"

    def __init__(self, transport: HTTPTransport) -> None:
        self._transport = transport

    async def create(self, *, session_id: str | None = None) -> dict[str, Any]:
        body = {"session_id": session_id} if session_id else {}
        return (await self._transport.request("POST", self._BASE, json=body)).json()

    async def get(self, thread_id: str) -> dict[str, Any]:
        return (await self._transport.request("GET", f"{self._BASE}/{thread_id}")).json()

    async def turn(
        self,
        thread_id: str,
        message: str,
        *,
        model_id: str | None = None,
        reasoning_option: str | None = None,
        max_tokens: int | None = None,
        kb_dataset_ids: list[str] | None = None,
        kb_mode: str = "off",
        kb_top_k: int = 5,
        kb_score_threshold: float = 0.4,
        web_search_enabled: bool = False,
        web_search_max_results: int = 5,
        file_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if model_id is not None:
            body["model_id"] = model_id
        if reasoning_option is not None:
            body["reasoning_option"] = reasoning_option
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        body.update(
            {
                "kb_dataset_ids": kb_dataset_ids or [],
                "kb_mode": kb_mode,
                "kb_top_k": kb_top_k,
                "kb_score_threshold": kb_score_threshold,
                "web_search_enabled": web_search_enabled,
                "web_search_max_results": web_search_max_results,
                "file_paths": file_paths or [],
            }
        )
        response = await self._transport.request(
            "POST", f"{self._BASE}/{thread_id}/turns", json=body
        )
        return response.json()

    async def interrupt(
        self, thread_id: str, turn_id: str, *, reason: str = "client_interrupt"
    ) -> dict[str, Any]:
        response = await self._transport.request(
            "POST",
            f"{self._BASE}/{thread_id}/turns/{turn_id}:interrupt",
            json={"reason": reason},
        )
        return response.json()

    async def events(
        self,
        thread_id: str,
        *,
        turn: dict[str, Any] | None = None,
        turn_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> AsyncIterator[StreamEvent]:
        cursor = turn.get("turn", turn) if isinstance(turn, dict) else {}
        events_url = cursor.get("events_url") if isinstance(cursor, dict) else None
        path = str(events_url or f"{self._BASE}/{thread_id}/events")
        params = None if events_url else {
            "after_sequence": after_sequence,
            "limit": limit,
            **({"turn_id": turn_id} if turn_id else {}),
        }
        async for event in self._transport.stream_sse_get(
            path,
            params=params,
        ):
            yield event
