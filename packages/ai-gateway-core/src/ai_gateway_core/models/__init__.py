"""Shared LLM-chat model types used across gateway and assistant-service.

Phase 5d extracted these out of ``assistant_service.core.models.model_registry``
so downstream modules (quiz, skills, streaming writer, …) can depend on the
dataclass without pulling in the full registry, and so gateway routes that
reference the chat-message shape never need a compile-time dep on
``assistant_service``.
"""

from .chat_message import ChatMessage, normalize_chat_message

__all__ = ["ChatMessage", "normalize_chat_message"]
