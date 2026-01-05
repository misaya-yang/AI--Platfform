"""
Role and Permission Management API

Provides endpoints for managing roles and permissions.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..deps import get_auth_context, get_dispatcher, AuthContext


router = APIRouter(prefix="/roles", tags=["roles"])


# ============================================================
# Request/Response Models
# ============================================================

class PermissionResponse(BaseModel):
    """Permission response model."""
    permission_code: str
    name: str
    description: Optional[str] = None
    category: str
    resource: str
    action: str
    is_system: bool = False


class RoleCreate(BaseModel):
    """Create role request."""
    role_name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z_]+$')
    description: str = Field(..., min_length=1, max_length=255)
    permissions: List[str] = Field(default=[])


class RoleUpdate(BaseModel):
    """Update role request."""
    description: Optional[str] = Field(None, min_length=1, max_length=255)
    permissions: Optional[List[str]] = None


class RoleResponse(BaseModel):
    """Role response model."""
    role_name: str
    description: Optional[str] = None
    permissions: List[str] = []
    is_system: bool = False
    user_count: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RoleListResponse(BaseModel):
    """Role list response."""
    roles: List[RoleResponse]
    total: int


class PermissionListResponse(BaseModel):
    """Permission list response."""
    permissions: List[PermissionResponse]
    total: int


class UserRoleAssignment(BaseModel):
    """User role assignment request."""
    user_id: str
    role_name: str


# ============================================================
# API Endpoints
# ============================================================

@router.get("", response_model=RoleListResponse)
async def list_roles(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    List all roles.

    Requires: role:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    roles = await db.list_roles()

    # Get user count for each role
    role_responses = []
    for role in roles:
        user_count = await db.get_role_user_count(role.get("role_name", ""))
        role_responses.append(RoleResponse(
            role_name=role.get("role_name", ""),
            description=role.get("description"),
            permissions=role.get("permissions", []),
            is_system=role.get("is_system", False),
            user_count=user_count,
            created_at=str(role.get("created_at")) if role.get("created_at") else None,
            updated_at=str(role.get("updated_at")) if role.get("updated_at") else None,
        ))

    return RoleListResponse(roles=role_responses, total=len(role_responses))


@router.post("", response_model=RoleResponse)
async def create_role(
    body: RoleCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new role.

    Requires: role:create permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:create")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    # Check if role already exists
    existing = await db.get_role(body.role_name)
    if existing:
        raise HTTPException(status_code=400, detail="Role already exists")

    # Validate permissions exist
    all_permissions = await db.list_permissions()
    valid_codes = {p.get("permission_code") for p in all_permissions}
    for perm in body.permissions:
        if perm not in valid_codes and perm != "admin:*":
            raise HTTPException(status_code=400, detail=f"Invalid permission: {perm}")

    await db.create_role(body.role_name, body.description, body.permissions)

    return RoleResponse(
        role_name=body.role_name,
        description=body.description,
        permissions=body.permissions,
        is_system=False,
        user_count=0,
        created_at=datetime.utcnow().isoformat(),
    )


@router.get("/permissions", response_model=PermissionListResponse)
async def list_permissions(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    category: Optional[str] = Query(None),
):
    """
    List all available permissions.

    Requires: role:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    permissions = await db.list_permissions(category=category)

    return PermissionListResponse(
        permissions=[PermissionResponse(**{k: v for k, v in p.items() if k in PermissionResponse.__fields__}) for p in permissions],
        total=len(permissions)
    )


@router.get("/{role_name}", response_model=RoleResponse)
async def get_role(
    role_name: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get role details.

    Requires: role:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    role = await db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    user_count = await db.get_role_user_count(role_name)

    return RoleResponse(
        role_name=role.get("role_name", ""),
        description=role.get("description"),
        permissions=role.get("permissions", []),
        is_system=role.get("is_system", False),
        user_count=user_count,
        created_at=str(role.get("created_at")) if role.get("created_at") else None,
        updated_at=str(role.get("updated_at")) if role.get("updated_at") else None,
    )


@router.put("/{role_name}", response_model=RoleResponse)
async def update_role(
    role_name: str,
    body: RoleUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Update role details.

    Cannot modify system roles.

    Requires: role:edit permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:edit")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    role = await db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    # Cannot modify system roles
    if role.get("is_system", False):
        raise HTTPException(status_code=400, detail="Cannot modify system role")

    # Validate permissions if provided
    if body.permissions is not None:
        all_permissions = await db.list_permissions()
        valid_codes = {p.get("permission_code") for p in all_permissions}
        for perm in body.permissions:
            if perm not in valid_codes and perm != "admin:*":
                raise HTTPException(status_code=400, detail=f"Invalid permission: {perm}")

    await db.update_role(
        role_name,
        description=body.description,
        permissions=body.permissions
    )

    updated_role = await db.get_role(role_name)
    user_count = await db.get_role_user_count(role_name)

    return RoleResponse(
        role_name=updated_role.get("role_name", ""),
        description=updated_role.get("description"),
        permissions=updated_role.get("permissions", []),
        is_system=updated_role.get("is_system", False),
        user_count=user_count,
        created_at=str(updated_role.get("created_at")) if updated_role.get("created_at") else None,
        updated_at=str(updated_role.get("updated_at")) if updated_role.get("updated_at") else None,
    )


@router.delete("/{role_name}")
async def delete_role(
    role_name: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Delete role.

    Cannot delete system roles or roles with assigned users.

    Requires: role:delete permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:delete")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    role = await db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    # Cannot delete system roles
    if role.get("is_system", False):
        raise HTTPException(status_code=400, detail="Cannot delete system role")

    # Check if role has assigned users
    user_count = await db.get_role_user_count(role_name)
    if user_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete role with {user_count} assigned user(s). Remove users first."
        )

    await db.delete_role(role_name)
    return {"status": "success", "message": f"Role {role_name} deleted"}


@router.get("/{role_name}/users")
async def get_role_users(
    role_name: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get users assigned to a role.

    Requires: role:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "role:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    role = await db.get_role(role_name)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    users = await db.get_users_by_role(role_name)

    return {
        "role_name": role_name,
        "users": users,
        "total": len(users)
    }
