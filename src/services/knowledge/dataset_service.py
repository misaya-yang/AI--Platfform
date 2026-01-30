"""Dataset management service for knowledge base.

This service handles dataset CRUD operations, permissions, and configuration.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage

logger = get_logger(__name__)


def _permission_rank(p: Optional[str]) -> int:
    """Get numeric rank for permission level."""
    if not p:
        return 0
    p = str(p).lower()
    return {"viewer": 1, "editor": 2, "owner": 3}.get(p, 0)


def _require_not_guest(user: UserContext) -> None:
    """Ensure user is not a guest."""
    if not user.is_authenticated or "guest" in (user.roles or []):
        raise PermissionDeniedError("Authentication required")


def _ensure_dict(value: Any) -> Dict[str, Any]:
    """Ensure value is a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class DatasetService:
    """Service for managing knowledge base datasets."""

    def __init__(self, settings: Settings, database: DatabaseStorage):
        self.settings = settings
        self.db = database

    # ========================================================================
    # Dataset CRUD Operations
    # ========================================================================

    async def list_datasets(self, user: UserContext) -> List[Dict[str, Any]]:
        """List all datasets accessible by the user."""
        _require_not_guest(user)

        rows = await self.db.get_datasets(user.user_id, user.tenant_id)
        results = []
        for row in rows:
            dataset_id = row.get("dataset_id") or row.get("id")
            permission = await self._effective_dataset_permission(user, dataset_id)
            if permission:
                results.append({
                    "id": dataset_id,
                    "name": row.get("name"),
                    "description": row.get("description"),
                    "embedding_model": row.get("embedding_model"),
                    "permission": permission,
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "document_count": row.get("document_count", 0),
                    "chunking_mode": row.get("chunking_mode", "auto"),
                    "is_public": row.get("is_public", False),
                })
        return results

    async def create_dataset(
        self, user: UserContext, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a new dataset."""
        _require_not_guest(user)

        name = data.get("name", "").strip()
        if not name:
            raise ValidationFailedError("Dataset name is required")

        # Validate dataset name - only allow alphanumeric, space, hyphen, underscore
        if not re.match(r'^[\w\s\-]+$', name):
            raise ValidationFailedError(
                "Dataset name can only contain letters, numbers, spaces, hyphens and underscores"
            )

        description = data.get("description", "").strip()

        # Extract embedding config
        embedding_config = _ensure_dict(data.get("embedding_config"))
        embedding_model = embedding_config.get("model") or data.get("embedding_model")
        if not embedding_model:
            from ..embedding import EMBEDDING_DIMENSIONS
            embedding_model = list(EMBEDDING_DIMENSIONS.keys())[0]

        # Extract chunking config
        chunking_config = _ensure_dict(data.get("chunking_config"))
        chunking_mode = chunking_config.get("mode", "auto")

        # Build dataset config
        config = {
            "embedding": embedding_config,
            "chunking": chunking_config,
            "search": _ensure_dict(data.get("search_config")),
            "multimodal": _ensure_dict(data.get("multimodal_config")),
            "ocr": _ensure_dict(data.get("ocr_config")),
            "retrieval": _ensure_dict(data.get("retrieval_config")),
        }

        dataset_id = await self.db.create_dataset(
            name=name,
            description=description,
            embedding_model=embedding_model,
            config=config,
            owner_id=user.user_id,
            tenant_id=user.tenant_id,
            is_public=data.get("is_public", False),
            chunking_mode=chunking_mode,
        )

        # Grant owner permission
        await self.db.grant_dataset_permission(
            dataset_id=dataset_id,
            user_id=user.user_id,
            permission="owner",
        )

        logger.info(f"Dataset created: {dataset_id} by {user.user_id}")

        return {
            "id": dataset_id,
            "name": name,
            "description": description,
            "embedding_model": embedding_model,
            "config": config,
            "permission": "owner",
        }

    async def update_dataset(
        self, user: UserContext, dataset_id: str, patch: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update dataset configuration."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "editor")

        dataset = await self._get_dataset_or_404(dataset_id)
        current_config = _ensure_dict(dataset.get("config"))

        # Build update fields
        updates = {}
        if "name" in patch:
            name = patch["name"].strip()
            if not name:
                raise ValidationFailedError("Dataset name cannot be empty")
            if not re.match(r'^[\w\s\-]+$', name):
                raise ValidationFailedError(
                    "Dataset name can only contain letters, numbers, spaces, hyphens and underscores"
                )
            updates["name"] = name

        if "description" in patch:
            updates["description"] = patch["description"].strip()

        if "is_public" in patch:
            updates["is_public"] = bool(patch["is_public"])

        # Update config sections
        config = current_config.copy()

        if "embedding_config" in patch:
            config["embedding"] = _ensure_dict(patch["embedding_config"])
            if config["embedding"].get("model"):
                updates["embedding_model"] = config["embedding"]["model"]

        if "chunking_config" in patch:
            config["chunking"] = _ensure_dict(patch["chunking_config"])

        if "search_config" in patch:
            config["search"] = _ensure_dict(patch["search_config"])

        if "multimodal_config" in patch:
            config["multimodal"] = _ensure_dict(patch["multimodal_config"])

        if "ocr_config" in patch:
            config["ocr"] = _ensure_dict(patch["ocr_config"])

        if "retrieval_config" in patch:
            config["retrieval"] = _ensure_dict(patch["retrieval_config"])

        updates["config"] = config

        await self.db.update_dataset(dataset_id, updates)
        logger.info(f"Dataset updated: {dataset_id}")

        return {"id": dataset_id, "updated": True}

    async def delete_dataset(self, user: UserContext, dataset_id: str) -> bool:
        """Delete a dataset and all its documents."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "owner")

        await self.db.delete_dataset(dataset_id)
        logger.info(f"Dataset deleted: {dataset_id}")
        return True

    async def get_dataset(self, user: UserContext, dataset_id: str) -> Dict[str, Any]:
        """Get dataset details."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "viewer")
        return await self._get_dataset_or_404(dataset_id)

    # ========================================================================
    # Permissions
    # ========================================================================

    async def list_dataset_permissions(
        self, user: UserContext, dataset_id: str
    ) -> List[Dict[str, Any]]:
        """List all permissions for a dataset."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "owner")
        return await self.db.list_dataset_permissions(dataset_id)

    async def grant_dataset_permission(
        self,
        user: UserContext,
        dataset_id: str,
        target_user_id: str,
        permission: str,
    ) -> Dict[str, Any]:
        """Grant permission to a user."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "owner")

        if permission not in ("viewer", "editor"):
            raise ValidationFailedError("Permission must be 'viewer' or 'editor'")

        await self.db.grant_dataset_permission(dataset_id, target_user_id, permission)
        return {"success": True}

    async def revoke_dataset_permission(
        self, user: UserContext, dataset_id: str, target_user_id: str
    ) -> Dict[str, Any]:
        """Revoke permission from a user."""
        _require_not_guest(user)
        await self._require_dataset_permission(user, dataset_id, "owner")

        await self.db.revoke_dataset_permission(dataset_id, target_user_id)
        return {"success": True}

    # ========================================================================
    # Statistics
    # ========================================================================

    async def get_dataset_statistics(self, dataset_id: str) -> Dict[str, Any]:
        """Get statistics for a dataset."""
        return await self.db.get_dataset_statistics(dataset_id)

    # ========================================================================
    # Internal Helpers
    # ========================================================================

    async def _get_dataset_or_404(self, dataset_id: str) -> Dict[str, Any]:
        """Get dataset or raise 404."""
        dataset = await self.db.get_dataset(dataset_id)
        if not dataset:
            from ...core.exceptions import NotFoundError
            raise NotFoundError(f"Dataset not found: {dataset_id}")
        return dataset

    async def _effective_dataset_permission(
        self, user: UserContext, dataset_id: str
    ) -> Optional[str]:
        """Get effective permission for user on dataset."""
        # Admin bypass
        if user.is_authenticated and (
            "admin" in (user.roles or []) or "system" in (user.roles or [])
        ):
            return "owner"

        # Check dataset visibility
        dataset = await self.db.get_dataset(dataset_id)
        if not dataset:
            return None

        is_public = dataset.get("is_public", False)

        # Check explicit permissions
        perms = await self.db.get_dataset_permissions(dataset_id)

        # Find user's permission
        user_perm = None
        for p in perms:
            if p.get("user_id") == user.user_id:
                user_perm = p.get("permission")
                break

        if user_perm:
            return user_perm

        # If public dataset, grant viewer access
        if is_public:
            return "viewer"

        return None

    async def _require_dataset_permission(
        self, user: UserContext, dataset_id: str, required: str
    ) -> None:
        """Require minimum permission level."""
        effective = await self._effective_dataset_permission(user, dataset_id)
        if not effective:
            raise PermissionDeniedError("You don't have access to this dataset")
        if _permission_rank(effective) < _permission_rank(required):
            raise PermissionDeniedError(
                f"This operation requires '{required}' permission"
            )

    async def require_dataset_access(
        self, user: UserContext, dataset_id: str
    ) -> Dict[str, Any]:
        """Check dataset access and return dataset."""
        _require_not_guest(user)
        effective = await self._effective_dataset_permission(user, dataset_id)
        if not effective:
            raise PermissionDeniedError("You don't have access to this dataset")
        return await self._get_dataset_or_404(dataset_id)
