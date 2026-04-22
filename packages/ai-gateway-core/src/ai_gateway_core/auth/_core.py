"""Concrete ``UserContext`` dataclass — shared by ai-gateway and
assistant-service.

Only the pure-data record lives here. Request-resolution machinery
(JWT decoding, API-key lookup, the ``UserResolver`` class) stays in the
gateway at ``src.core.auth.user_resolver`` — the assistant process
never re-does that work; it trusts the gateway-forwarded ``X-User-*``
headers and builds a ``UserContext`` directly from them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UserContext:
    """Per-request authenticated-user context.

    Fields align with the Protocol in ``ai_gateway_core.auth.UserContextLike``
    so concrete instances structurally satisfy the protocol without any
    explicit registration.
    """

    user_id: str
    tenant_id: str = ""
    tier: str = "anonymous"  # anonymous | normal | premium | enterprise | admin
    is_authenticated: bool = False
    ip: str = ""
    roles: list[str] = field(default_factory=list)


__all__ = ["UserContext"]
