"""Backward-compat shim. Exceptions moved to ``ai_gateway_core.exceptions``
as part of the Assistant Service True Isolation migration (phase 2).
Import from ``ai_gateway_core.exceptions`` directly in new code.
"""

from ai_gateway_core.exceptions import (
    AdapterNotFoundError,
    AuthError,
    AuthenticationRequiredError,
    CircuitBreakerOpenError,
    GatewayError,
    InvalidContentTypeError,
    NoHealthyInstanceError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitExceededError,
    ServiceNotFoundError,
    TaskCancelledError,
    TaskNotFoundError,
    ValidationFailedError,
)

__all__ = [
    "AdapterNotFoundError",
    "AuthError",
    "AuthenticationRequiredError",
    "CircuitBreakerOpenError",
    "GatewayError",
    "InvalidContentTypeError",
    "NoHealthyInstanceError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitExceededError",
    "ServiceNotFoundError",
    "TaskCancelledError",
    "TaskNotFoundError",
    "ValidationFailedError",
]
