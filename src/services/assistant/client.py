"""
Assistant Client Protocol — abstraction layer for in-process or remote assistant.

Enables the Gateway to call the assistant service either:
- In-process (current behavior, zero overhead)
- Remote (HTTP + SSE to a separate microservice)

Usage:
    client = InProcessAssistantClient(service)   # or RemoteAssistantClient(url)
    result = await client.chat(user, session_id, message, config)
    async for event in client.chat_stream(user, session_id, message, config):
        ...
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ChatRequest:
    """Unified chat request for both in-process and remote."""
    message: str
    session_id: str
    model_id: str = "qwen3.5-plus"
    temperature: float = 0.7
    max_tokens: int | None = None
    kb_dataset_ids: list[str] | None = None
    kb_mode: str = "auto"
    web_search_enabled: bool = False
    system_prompt: str | None = None
    history: list[dict[str, str]] | None = None
    file_paths: list[str] | None = None
    enable_task_planning: bool = False
    memory_mode: str | None = None
    # Additional config fields passed through
    extra: dict[str, Any] | None = None


@dataclass
class ChatResponse:
    """Unified chat response."""
    content: str
    usage: dict[str, Any] | None = None
    contexts: list[dict] | None = None
    duration_ms: float | None = None
    model_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None


@dataclass
class StreamEvent:
    """A single SSE event from chat_stream."""
    event_type: str
    data: Any
    timestamp: float | None = None


@runtime_checkable
class AssistantClientProtocol(Protocol):
    """Protocol defining the assistant client interface.

    Both InProcessAssistantClient and RemoteAssistantClient implement this.
    The Gateway API layer uses this protocol, making it agnostic to deployment topology.
    """

    async def chat(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Non-streaming chat completion."""
        ...

    async def chat_stream(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming chat completion (yields StreamEvent)."""
        ...

    def get_available_models(self) -> list[dict[str, Any]]:
        """List available LLM models."""
        ...


class InProcessAssistantClient:
    """Calls AssistantService directly (in-process). Zero overhead.

    This is the default mode — identical to pre-refactor behavior.
    """

    def __init__(self, service: Any) -> None:
        self.service = service

    async def chat(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return await self.service.chat(
            user=user,
            session_id=session_id,
            message=message,
            config=config,
            history=history,
        )

    async def chat_stream(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        async for event in self.service.chat_stream(
            user=user,
            session_id=session_id,
            message=message,
            config=config,
            history=history,
        ):
            yield StreamEvent(
                event_type=event.event_type,
                data=event.data,
                timestamp=event.timestamp,
            )

    def get_available_models(self) -> list[dict[str, Any]]:
        return self.service.get_available_models()


class RemoteAssistantClient:
    """Calls assistant-service over HTTP + SSE. For microservice deployment.

    The assistant-service runs as a separate container (port 8093).
    SSE streaming is proxied transparently.
    """

    def __init__(self, base_url: str = "http://assistant-service:8093") -> None:
        self.base_url = base_url.rstrip("/")

    async def chat(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        import httpx

        payload = self._build_payload(user, session_id, message, config, history)
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{self.base_url}/api/v1/assistant/chat", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def chat_stream(
        self,
        user: Any,
        session_id: str,
        message: str,
        config: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import httpx

        payload = self._build_payload(user, session_id, message, config, history)
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/v1/assistant/chat/stream",
                json=payload,
            ) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        for line in event_str.split("\n"):
                            if line.startswith("data: "):
                                data_str = line[6:]
                                if data_str == "[DONE]":
                                    return
                                try:
                                    event_data = json.loads(data_str)
                                    yield StreamEvent(
                                        event_type=event_data.get("event_type", ""),
                                        data=event_data.get("data"),
                                        timestamp=event_data.get("timestamp", time.time()),
                                    )
                                except json.JSONDecodeError:
                                    pass

    def get_available_models(self) -> list[dict[str, Any]]:
        import httpx

        resp = httpx.get(f"{self.base_url}/api/v1/assistant/models", timeout=10.0)
        if resp.is_success:
            data = resp.json()
            return data.get("models", [])
        return []

    def _build_payload(
        self, user: Any, session_id: str, message: str, config: Any, history: list | None,
    ) -> dict:
        """Build JSON payload for remote call, forwarding user context."""
        payload: dict[str, Any] = {
            "message": message,
            "session_id": session_id,
            "model_id": getattr(config, "model_id", "qwen3.5-plus"),
            "temperature": getattr(config, "temperature", 0.7),
            "max_tokens": getattr(config, "max_tokens", None),
            "kb_dataset_ids": getattr(config, "kb_dataset_ids", None),
            "kb_mode": getattr(config, "kb_mode", "auto"),
            "web_search_enabled": getattr(config, "web_search_enabled", False),
            "system_prompt": getattr(config, "system_prompt", None),
            "enable_task_planning": getattr(config, "enable_task_planning", False),
            "memory_mode": getattr(config, "memory_mode", None),
        }
        if history:
            payload["history"] = history

        # Forward user context as headers (handled by assistant-service auth middleware)
        payload["_user_context"] = {
            "user_id": getattr(user, "user_id", "anonymous"),
            "tenant_id": getattr(user, "tenant_id", ""),
            "roles": getattr(user, "roles", []),
            "tier": getattr(user, "tier", "normal"),
        }
        return payload


def create_assistant_client(
    mode: str = "in_process",
    service: Any | None = None,
    remote_url: str = "http://assistant-service:8093",
) -> AssistantClientProtocol:
    """Factory: create the appropriate client based on deployment mode.

    Args:
        mode: "in_process" (default) or "remote"
        service: AssistantService instance (required for in_process)
        remote_url: URL of assistant-service (required for remote)
    """
    if mode == "remote":
        logger.info(f"Creating RemoteAssistantClient → {remote_url}")
        return RemoteAssistantClient(base_url=remote_url)

    if service is None:
        raise ValueError("InProcessAssistantClient requires a service instance")
    logger.info("Creating InProcessAssistantClient (in-process mode)")
    return InProcessAssistantClient(service=service)
