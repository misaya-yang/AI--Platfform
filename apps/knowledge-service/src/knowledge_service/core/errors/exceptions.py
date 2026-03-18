"""Shim: re-export from core.exceptions for alternative import path."""
from knowledge_service.core.exceptions import (
    GatewayError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ValidationFailedError,
)

__all__ = [
    "GatewayError",
    "NotFoundError",
    "PermissionDeniedError",
    "ValidationFailedError",
    "RateLimitError",
]
