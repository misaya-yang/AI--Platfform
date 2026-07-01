"""Security primitives shared across gateway and microservices."""

from .redaction import REDACTION_PATTERNS, SENSITIVE_KEY_RE, redact_trace_text
from .safe_fetch import (
    SafeFetchError,
    SafeFetchResponse,
    is_safe_destination,
    safe_callback_post,
    safe_fetch,
    safe_fetch_with_response,
    validate_callback_url,
)

__all__ = [
    "REDACTION_PATTERNS",
    "SENSITIVE_KEY_RE",
    "SafeFetchError",
    "SafeFetchResponse",
    "is_safe_destination",
    "redact_trace_text",
    "safe_callback_post",
    "safe_fetch",
    "safe_fetch_with_response",
    "validate_callback_url",
]
