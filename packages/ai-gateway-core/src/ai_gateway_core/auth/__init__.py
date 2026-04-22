"""Authentication contract.

Concrete ``UserContext`` lives in each service. This module exports a
structural protocol so code that only reads identity can depend on the
contract instead of the concrete type.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class UserContextLike(Protocol):
    """Minimal contract for an authenticated-user value passed between services.

    Concrete implementations in ai-gateway and assistant-service must
    expose at least these attributes. Additional fields (permissions,
    tenant metadata, etc.) are implementation-defined.
    """

    user_id: str
    tenant_id: str | None


__all__ = ["UserContextLike"]
