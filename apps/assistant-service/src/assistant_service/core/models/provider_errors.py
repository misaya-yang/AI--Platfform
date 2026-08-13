"""Prompt-safe provider stream error vocabulary."""

from __future__ import annotations


class ProviderStreamError(RuntimeError):
    """Prompt-safe provider stream failure surfaced to the runtime boundary."""

    # Preserve the historical exception path for reflection and serialization.
    __module__ = "assistant_service.core.models.model_registry"

    def __init__(self, provider: str, error_type: str) -> None:
        self.provider = provider
        self.error_type = error_type
        super().__init__(f"{provider} stream failed ({error_type})")


_SAFE_ANTHROPIC_ERROR_TYPES = frozenset(
    {
        "api_error",
        "authentication_error",
        "billing_error",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
    }
)

_SAFE_OPENAI_FINISH_REASONS = frozenset(
    {"stop", "length", "tool_calls", "content_filter", "function_call"}
)
_SAFE_OPENAI_ERROR_TYPES = frozenset(
    {
        "authentication_error",
        "invalid_request_error",
        "not_found_error",
        "permission_error",
        "rate_limit_error",
        "server_error",
    }
)
_SAFE_ANTHROPIC_STOP_REASONS = frozenset(
    {
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "model_context_window_exceeded",
    }
)
_SAFE_GOOGLE_FINISH_REASONS = frozenset(
    {
        "STOP",
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "MALFORMED_FUNCTION_CALL",
        "IMAGE_SAFETY",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
    }
)

_SAFE_GOOGLE_BLOCK_REASONS = frozenset(
    {
        "BLOCKLIST",
        "IMAGE_SAFETY",
        "OTHER",
        "PROHIBITED_CONTENT",
        "SAFETY",
    }
)
