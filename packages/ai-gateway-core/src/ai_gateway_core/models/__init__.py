"""Shared LLM-chat model types used across gateway and assistant-service.

Phase 5d extracted these out of ``assistant_service.core.models.model_registry``
so downstream modules (quiz, skills, streaming writer, …) can depend on the
dataclass without pulling in the full registry, and so gateway routes that
reference the chat-message shape never need a compile-time dep on
``assistant_service``.
"""

from .capabilities import (
    RESERVED_OPTION_IDS,
    ModelCapabilityError,
    ResolvedReasoningOption,
    get_builtin_model_capabilities,
    get_model_capability_adapter,
    list_model_capability_adapters,
    merge_model_capability_profiles,
    resolve_reasoning_option,
    safe_model_capability_profile,
    validate_model_capability_profile,
)
from .chat_message import ChatMessage, normalize_chat_message

__all__ = [
    "ChatMessage",
    "ModelCapabilityError",
    "RESERVED_OPTION_IDS",
    "ResolvedReasoningOption",
    "get_builtin_model_capabilities",
    "get_model_capability_adapter",
    "list_model_capability_adapters",
    "merge_model_capability_profiles",
    "normalize_chat_message",
    "resolve_reasoning_option",
    "safe_model_capability_profile",
    "validate_model_capability_profile",
]
