"""
User Management API - CRUD operations for users

Provides endpoints for managing user accounts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, validator

from ..deps import get_auth_context, get_dispatcher, get_user_context, AuthContext
from ...core.auth.user_resolver import UserContext
from ...core.auth.password import (
    ALLOWED_EMAIL_DOMAIN,
    DEFAULT_PASSWORD,
    extract_username_from_email,
    hash_password,
    validate_email,
)


router = APIRouter(prefix="/users", tags=["users"])


# ============================================================
# Request/Response Models
# ============================================================

class UserCreate(BaseModel):
    """Create user request."""
    email: str
    display_name: str = Field(..., min_length=1, max_length=255)
    department: Optional[str] = Field(None, max_length=50, description="Department: cs, sales, tech, admin")
    roles: List[str] = Field(default=["user"])

    @validator("email")
    def normalize_email_domain(cls, v):
        raw = (v or "").strip()
        if not raw:
            raise ValueError("Email is required")
        if "@" not in raw:
            raw = f"{raw}@{ALLOWED_EMAIL_DOMAIN}"
        errors = validate_email(raw)
        if errors:
            raise ValueError("; ".join(errors))
        return raw.lower()

    @validator("department")
    def validate_department(cls, v):
        if v is not None:
            allowed = ["cs", "sales", "tech", "admin"]
            if v not in allowed:
                raise ValueError(f"Department must be one of: {', '.join(allowed)}")
        return v


class UserUpdate(BaseModel):
    """Update user request."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)
    department: Optional[str] = Field(None, max_length=50, description="Department: cs, sales, tech, admin")
    roles: Optional[List[str]] = None
    extra_permissions: Optional[List[str]] = Field(None, description="Extra permissions (directly assigned, not through roles)")
    status: Optional[str] = None
    tier: Optional[str] = None

    @validator("department")
    def validate_department(cls, v):
        if v is not None:
            allowed = ["cs", "sales", "tech", "admin"]
            if v not in allowed:
                raise ValueError(f"Department must be one of: {', '.join(allowed)}")
        return v


class ProfileUpdate(BaseModel):
    """Update own profile request (limited fields - no roles/permissions)."""
    display_name: Optional[str] = Field(None, min_length=1, max_length=255)


class UserResponse(BaseModel):
    """User response model."""
    user_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    username: Optional[str] = None
    department: Optional[str] = None
    roles: List[str] = []
    extra_permissions: List[str] = []
    status: str = "active"
    tier: str = "normal"
    force_password_change: bool = True
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None


class UserListResponse(BaseModel):
    """User list response with pagination."""
    users: List[UserResponse]
    total: int
    page: int
    page_size: int


# ============================================================
# API Endpoints
# ============================================================

@router.get("", response_model=UserListResponse)
async def list_users(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """
    List all users with pagination.

    Requires: user:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    offset = (page - 1) * page_size
    users, total = await db.list_users_paginated(
        status=status, search=search, limit=page_size, offset=offset
    )

    return UserListResponse(
        users=[UserResponse(**{k: v for k, v in u.items() if k in UserResponse.__fields__}) for u in users],
        total=total,
        page=page,
        page_size=page_size
    )


@router.post("", response_model=UserResponse)
async def create_user(
    body: UserCreate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Create a new user.

    - User is created with default password (111111)
    - User must change password on first login

    Requires: user:create permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:create")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    # Check if email already exists
    existing = await db.get_user_by_email(body.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    # Generate user_id from email
    user_id = extract_username_from_email(body.email)

    # Check if user_id already exists
    existing_id = await db.get_user(user_id)
    if existing_id:
        # Add suffix to make unique
        import uuid
        user_id = f"{user_id}_{uuid.uuid4().hex[:6]}"

    # Create user with default password
    password_hash = hash_password(DEFAULT_PASSWORD)

    user_data = {
        "user_id": user_id,
        "email": body.email,
        "display_name": body.display_name,
        "department": body.department,
        "username": user_id,
        "roles": body.roles,
        "password_hash": password_hash,
        "force_password_change": True,
        "status": "active",
        "tier": "normal",
        "tenant_id": "default",
        "created_by": auth.user_id,
    }

    await db.save_user_with_password(user_data)

    # Assign roles in user_roles table
    for role in body.roles:
        await db.assign_user_role(user_id, role, auth.user_id)

    return UserResponse(
        user_id=user_id,
        email=body.email,
        display_name=body.display_name,
        department=body.department,
        username=user_id,
        roles=body.roles,
        status="active",
        tier="normal",
        force_password_change=True,
        last_login_at=None,
        created_at=datetime.utcnow().isoformat()
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Get user details.

    Requires: user:list permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:list")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get extra permissions
    extra_perms = await db.get_user_extra_permissions(user_id)
    extra_perm_codes = [p.get("permission_code") for p in extra_perms]

    response_data = {k: v for k, v in user.items() if k in UserResponse.__fields__}
    response_data["extra_permissions"] = extra_perm_codes
    return UserResponse(**response_data)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdate,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Update user details.

    Requires: user:edit permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:edit")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Build update dict (exclude extra_permissions as it's handled separately)
    update_data = body.dict(exclude_unset=True, exclude={"extra_permissions"})
    if update_data:
        await db.update_user(user_id, update_data)

        # Update roles if changed
        if body.roles is not None:
            await db.update_user_roles(user_id, body.roles, auth.user_id)

    # Update extra permissions if provided
    if body.extra_permissions is not None:
        await db.update_user_extra_permissions(user_id, body.extra_permissions, auth.user_id)

    updated_user = await db.get_user(user_id)

    # Get extra permissions
    extra_perms = await db.get_user_extra_permissions(user_id)
    extra_perm_codes = [p.get("permission_code") for p in extra_perms]

    response_data = {k: v for k, v in updated_user.items() if k in UserResponse.__fields__}
    response_data["extra_permissions"] = extra_perm_codes
    return UserResponse(**response_data)


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Delete user.

    Cannot delete self or system admin.

    Requires: user:delete permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:delete")

    # Cannot delete self
    if user_id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    # Cannot delete system admin
    if user_id == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete system admin")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.delete_user(user_id)
    return {"status": "success", "message": f"User {user_id} deleted"}


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Reset user password to default (111111).

    User will be required to change password on next login.

    Requires: user:edit permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:edit")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    password_hash = hash_password(DEFAULT_PASSWORD)
    await db.reset_user_password(user_id, password_hash)

    return {"status": "success", "message": f"Password reset to default for user {user_id}"}


@router.post("/{user_id}/enable")
async def enable_user(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Enable a disabled user.

    Requires: user:edit permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:edit")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.update_user(user_id, {"status": "active"})
    return {"status": "success", "message": f"User {user_id} enabled"}


@router.post("/{user_id}/disable")
async def disable_user(
    user_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Disable a user account.

    Cannot disable self or system admin.

    Requires: user:edit permission
    """
    dispatcher = get_dispatcher(request)
    dispatcher.rbac.require(auth.roles, "user:edit")

    # Cannot disable self
    if user_id == auth.user_id:
        raise HTTPException(status_code=400, detail="Cannot disable your own account")

    # Cannot disable system admin
    if user_id == "admin":
        raise HTTPException(status_code=400, detail="Cannot disable system admin")

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.update_user(user_id, {"status": "disabled"})
    return {"status": "success", "message": f"User {user_id} disabled"}


@router.put("/{user_id}/profile", response_model=UserResponse)
async def update_user_profile(
    user_id: str,
    body: ProfileUpdate,
    request: Request,
    current_user: UserContext = Depends(get_user_context),
):
    """
    Update user's own profile (limited to display_name).

    Users can only update their own profile.
    For admin-level changes (roles, permissions), use PUT /{user_id} instead.
    """
    # Users can only update their own profile
    if current_user.user_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Can only update your own profile"
        )

    db = getattr(request.app.state, "database", None)
    if not db or not getattr(db, "enabled", False):
        raise HTTPException(status_code=503, detail="Database not available")

    user = await db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Only update allowed fields
    update_data = body.dict(exclude_unset=True)
    if update_data:
        await db.update_user(user_id, update_data)

    updated_user = await db.get_user(user_id)

    # Get extra permissions
    extra_perms = await db.get_user_extra_permissions(user_id)
    extra_perm_codes = [p.get("permission_code") for p in extra_perms]

    response_data = {k: v for k, v in updated_user.items() if k in UserResponse.__fields__}
    response_data["extra_permissions"] = extra_perm_codes
    return UserResponse(**response_data)
