from __future__ import annotations

from typing import Dict, List, Set

from ..exceptions import PermissionDeniedError


class RBAC:
    def __init__(self, role_permissions: Dict[str, List[str]]):
        self.role_permissions = role_permissions

    def permissions_for_roles(self, roles: List[str]) -> Set[str]:
        permissions: Set[str] = set()
        for role in roles:
            permissions.update(self.role_permissions.get(role, []))
        return permissions

    def has_permission(self, roles: List[str], permission: str) -> bool:
        permissions = self.permissions_for_roles(roles)
        if "admin:*" in permissions:
            return True
        if permission in permissions:
            return True
        prefix = permission.split(":")[0] + ":*"
        return prefix in permissions

    def require(self, roles: List[str], permission: str) -> None:
        if not self.has_permission(roles, permission):
            raise PermissionDeniedError(f"Missing permission: {permission}")
