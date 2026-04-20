"""
Chat module — streaming and non-streaming chat completions.

Usage::

    async with AssistantClient("my-key") as client:
        # Non-streaming
        resp = await client.chat.send("Hello!")

        # Streaming
        async for event in client.chat.stream("Tell me a story"):
            if event.is_text():
                print(event.text, end="", flush=True)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ai_assistant.models.events import StreamEvent
from ai_assistant.models.request import ChatRequest
from ai_assistant.models.response import ChatResponse
from ai_assistant.transport.http import HTTPTransport


class ChatModule:
    """High-level chat operations backed by ``/api/v1/assistant/chat``."""

    _CHAT_PATH = "/api/v1/assistant/chat"
    _STREAM_PATH = "/api/v1/assistant/chat/stream"
    _CANCEL_PATH = "/api/v1/assistant/tasks/{task_id}/cancel"

    def __init__(self, transport: HTTPTransport) -> None:
        self._transport = transport

    async def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model_id: str = "qwen3.6-plus",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        kb_dataset_ids: list[str] | None = None,
        web_search_enabled: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        """Send a non-streaming chat request and return the full response.

        All keyword arguments are forwarded to ``ChatRequest``.
        """
        req = ChatRequest(
            message=message,
            session_id=session_id,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            kb_dataset_ids=kb_dataset_ids,
            web_search_enabled=web_search_enabled,
            stream=False,
            **kwargs,
        )
        resp = await self._transport.request("POST", self._CHAT_PATH, json=req.to_dict())
        return ChatResponse.from_dict(resp.json())

    async def stream(
        self,
        message: str,
        *,
        session_id: str | None = None,
        model_id: str = "qwen3.6-plus",
        temperature: float = 0.7,
        max_tokens: int | None = None,
        system_prompt: str | None = None,
        kb_dataset_ids: list[str] | None = None,
        web_search_enabled: bool = False,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Start a streaming chat and yield ``StreamEvent`` objects.

        The returned async iterator stays open until the server sends a
        ``done`` event or closes the connection.
        """
        req = ChatRequest(
            message=message,
            session_id=session_id,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            kb_dataset_ids=kb_dataset_ids,
            web_search_enabled=web_search_enabled,
            stream=True,
            **kwargs,
        )
        async for event in self._transport.stream_sse(self._STREAM_PATH, req.to_dict()):
            yield event

    async def cancel(self, task_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Cancel a running chat task.

        Args:
            task_id: The ``task_id`` from the ``run_started`` stream event.
            reason: Optional human-readable cancellation reason.

        Returns:
            Server response dict with cancellation status.
        """
        path = self._CANCEL_PATH.format(task_id=task_id)
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        resp = await self._transport.request("POST", path, json=body)
        return resp.json()
