"""Security primitives shared across gateway and microservices."""

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
    "SafeFetchError",
    "SafeFetchResponse",
    "is_safe_destination",
    "safe_callback_post",
    "safe_fetch",
    "safe_fetch_with_response",
    "validate_callback_url",
]
