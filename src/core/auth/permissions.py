from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from ..exceptions import PermissionDeniedError
from .rbac import RBAC


class Capability(str, Enum):
    """Gateway capability abstraction over concrete permission codes."""

    AGENT_INVOKE = "AgentInvoke"
    SERVICE_LIST_READ = "ServiceListRead"
    SERVICE_CONFIG_WRITE = "ServiceConfigWrite"


_CAPABILITY_PERMISSIONS: Dict[Capability, str] = {
    Capability.AGENT_INVOKE: "conversation:playground:access",
    Capability.SERVICE_LIST_READ: "console:services:view",
    Capability.SERVICE_CONFIG_WRITE: "console:services:edit",
}

_CAPABILITY_LEGACY_ALIASES: Dict[Capability, Tuple[str, ...]] = {
    Capability.AGENT_INVOKE: ("service:invoke",),
    Capability.SERVICE_LIST_READ: ("service:view",),
    Capability.SERVICE_CONFIG_WRITE: ("service:manage",),
}


@dataclass(frozen=True)
class CapabilityDecision:
    capability: Capability
    required_permission: str
    accepted_permissions: Tuple[str, ...]
    matched_permission: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return bool(self.matched_permission)


def canonical_permission(capability: Capability) -> str:
    return _CAPABILITY_PERMISSIONS[capability]


def accepted_permissions(capability: Capability) -> Tuple[str, ...]:
    canonical = canonical_permission(capability)
    aliases = _CAPABILITY_LEGACY_ALIASES.get(capability, ())
    return (canonical, *aliases)


def _merge_subjects(roles: Sequence[str], permissions: Optional[Sequence[str]]) -> List[str]:
    subjects: List[str] = []
    for raw in [*(roles or []), *((permissions or []))]:
        value = str(raw or "").strip()
        if value and value not in subjects:
            subjects.append(value)
    return subjects


def check_capability(
    *,
    rbac: RBAC,
    roles: Sequence[str],
    permissions: Optional[Sequence[str]],
    capability: Capability,
) -> CapabilityDecision:
    required = canonical_permission(capability)
    accepted = accepted_permissions(capability)
    subjects = _merge_subjects(roles, permissions)

    for permission in accepted:
        if rbac.has_permission(subjects, permission):
            return CapabilityDecision(
                capability=capability,
                required_permission=required,
                accepted_permissions=accepted,
                matched_permission=permission,
            )

    return CapabilityDecision(
        capability=capability,
        required_permission=required,
        accepted_permissions=accepted,
        matched_permission=None,
    )


def require_capability(
    *,
    rbac: RBAC,
    roles: Sequence[str],
    permissions: Optional[Sequence[str]],
    capability: Capability,
) -> CapabilityDecision:
    decision = check_capability(
        rbac=rbac,
        roles=roles,
        permissions=permissions,
        capability=capability,
    )
    if decision.allowed:
        return decision
    raise PermissionDeniedError(
        f"Missing capability {capability.value}: {decision.required_permission}"
    )


def build_permission_denied_detail(
    *,
    capability: Capability,
    trace_id: str = "",
    message: Optional[str] = None,
) -> Dict[str, object]:
    required = canonical_permission(capability)
    return {
        "message": message or f"Permission denied: {required} required",
        "required_capability": capability.value,
        "required_permission": required,
        "accepted_permissions": list(accepted_permissions(capability)),
        "trace_id": trace_id,
    }
