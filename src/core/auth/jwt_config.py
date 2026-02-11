"""
Unified JWT configuration module.

This module provides centralized JWT configuration to ensure consistency
between token signing (auth.py) and verification (deps.py).
"""

import logging

logger = logging.getLogger(__name__)

# Default fallback values
DEFAULT_JWT_SECRET = "default-secret-change-me"
DEFAULT_JWT_ALGORITHM = "HS256"

_warned_default_secret = False


def get_jwt_secret(configured_secret: str | None) -> str:
    """
    Get JWT secret with fallback and warning for default value.

    Args:
        configured_secret: The secret from settings.authentication.jwt.secret

    Returns:
        The actual secret to use for JWT operations
    """
    global _warned_default_secret

    secret = configured_secret or DEFAULT_JWT_SECRET

    if secret == DEFAULT_JWT_SECRET and not _warned_default_secret:
        logger.warning(
            "Using default JWT secret! This is insecure for production. "
            "Please set GATEWAY_AUTHENTICATION__JWT__SECRET in your environment."
        )
        _warned_default_secret = True

    return secret


def get_jwt_algorithms(configured_algorithms: list[str] | None) -> list[str]:
    """
    Get JWT algorithms with fallback.

    Args:
        configured_algorithms: The algorithms from settings.authentication.jwt.algorithms

    Returns:
        List of algorithms to use for JWT operations
    """
    if configured_algorithms and len(configured_algorithms) > 0:
        return configured_algorithms
    return [DEFAULT_JWT_ALGORITHM]
