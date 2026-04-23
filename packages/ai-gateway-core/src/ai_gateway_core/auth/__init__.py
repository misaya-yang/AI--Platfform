"""Authentication contract + concrete user context.

- ``UserContextLike`` — structural protocol; use for type annotations
  inside business code.
- ``UserContext`` — concrete dataclass; construct instances of this to
  pass around. Lives here so both services can build identical records
  from their respective auth paths (gateway: JWT/API key; assistant:
  X-User-* header trust).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._core import UserContext
from .gateway_secret import (
    GatewaySecret,
    InMemoryReplayStore,
    InvalidGatewaySecret,
    ReplayStore,
)


@runtime_checkable
class UserContextLike(Protocol):
    """Minimal contract for an authenticated-user value passed between services."""

    user_id: str
    tenant_id: str | None


__all__ = [
    "GatewaySecret",
    "InMemoryReplayStore",
    "InvalidGatewaySecret",
    "ReplayStore",
    "UserContext",
    "UserContextLike",
]
