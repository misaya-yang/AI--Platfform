"""Lightweight auth that trusts headers forwarded by the API Gateway.

The Gateway authenticates users and forwards identity as X-User-* headers.
This module extracts those headers into a UserContext dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class UserContext:
    """Resolved identity from gateway-forwarded headers."""

    user_id: str
    tenant_id: str
    user_tier: str = "normal"
    user_type: str = "user"
    roles: list = field(default_factory=lambda: ["user"])
    ip: str = ""
    is_authenticated: bool = True
    # ---- App-scoped identity (image-redesign Phase 2) -------------------
    # When the API caller is itself a multi-tenant app proxying its own
    # users to us, it forwards X-App-Tenant-Id and X-App-User-Id. These
    # don't replace user_id/tenant_id — those still identify *the API
    # client's* JWT subject. owner_scope (computed elsewhere) combines
    # all three so artifact / image-session lookups isolate per end-user.
    app_user_id: str | None = None
    app_tenant_id: str | None = None

    @property
    def tier(self) -> str:
        return self.user_tier

    @property
    def role(self) -> str:
        return self.roles[0] if self.roles else "user"

    @property
    def is_anonymous(self) -> bool:
        return self.user_id == "anonymous"


ANONYMOUS_CONTEXT = UserContext(
    user_id="anonymous",
    tenant_id="default",
    user_tier="anonymous",
    user_type="anonymous",
)


async def get_user_context(request: Request) -> UserContext:
    """FastAPI dependency: resolve user from gateway-forwarded headers.

    Roles come through ``X-User-Roles`` as a comma-separated list. The
    gateway's JWT layer produced the list; tool_registry consumes it
    for ``role:admin`` / ``role:premium-analyst`` authz. Without this
    parsing every request would default to ``["user"]`` and admin-only
    tools would be silently unavailable (Audit Finding H-2).
    """
    user_id = request.headers.get("X-User-Id", "").strip()
    tenant_id = request.headers.get("X-Tenant-Id", "").strip()
    user_tier = request.headers.get("X-User-Tier", "normal").strip()
    user_type = request.headers.get("X-User-Type", "user").strip()
    roles_raw = request.headers.get("X-User-Roles", "").strip()
    roles = [r.strip() for r in roles_raw.split(",") if r.strip()] if roles_raw else ["user"]
    app_user_id = request.headers.get("X-App-User-Id", "").strip() or None
    app_tenant_id = request.headers.get("X-App-Tenant-Id", "").strip() or None

    if user_id and tenant_id:
        return UserContext(
            user_id=user_id,
            tenant_id=tenant_id,
            user_tier=user_tier,
            user_type=user_type,
            roles=roles,
            app_user_id=app_user_id,
            app_tenant_id=app_tenant_id,
        )

    # Allow anonymous in dev/internal calls
    settings = request.app.state.settings
    if settings.app.allow_anonymous:
        return ANONYMOUS_CONTEXT

    raise HTTPException(
        status_code=401,
        detail="Missing identity headers (X-User-Id, X-Tenant-Id). Route through Gateway.",
    )
