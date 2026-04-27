"""Security primitives shared across gateway and microservices."""

from .safe_fetch import (
    SafeFetchError,
    is_safe_destination,
    safe_fetch,
    validate_callback_url,
)

__all__ = [
    "SafeFetchError",
    "is_safe_destination",
    "safe_fetch",
    "validate_callback_url",
]
