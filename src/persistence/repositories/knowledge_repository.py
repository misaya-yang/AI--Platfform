"""
Knowledge Base (KBMS) repository

Provides data access abstractions for datasets/documents/segments and permissions.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from .base import BaseRepository

logger = logging.getLogger(__name__)


class KnowledgeRepository(ABC):
    # ========= Dataset =========

    @abstractmethod
    async def save_dataset(self, dataset: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def list_datasets(
        self,
        tenant_id: str | None = None,
        include_public: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def delete_dataset(
        self,
        dataset_id: str,
        deleted_by: str | None = None,
        delete_reason: str | None = None,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def clear_dataset_needs_reindex(self, dataset_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_datasets_statistics_batch(
        self, dataset_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        raise NotImplementedError

    @abstractmethod
    async def dataset_has_embeddings(self, dataset_id: str) -> bool:
        raise NotImplementedError

    # ========= Permissions =========

    @abstractmethod
    async def grant_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str, permission: str
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def revoke_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def list_dataset_permissions(self, dataset_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    # ========= Document =========

    @abstractmethod
    async def save_document(self, document: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def list_documents(
        self,
        dataset_id: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: float | None = None,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_document(self, document_id: str) -> bool:
        raise NotImplementedError

    # ========= Segment =========

    @abstractmethod
    async def insert_segments(self, segments: list[dict[str, Any]]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_segments_by_document(self, document_id: str) -> int:
        raise NotImplementedError

    @abstractmethod
    async def list_segments(
        self,
        dataset_id: str,
        document_id: str | None = None,
        query_text: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_segment(self, segment_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def update_segment(
        self,
        segment_id: str,
        text: str,
        token_count: int | None = None,
        metadata: dict[str, Any] | None = None,
        vector_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_segment(self, segment_id: str) -> bool:
        raise NotImplementedError


class DatabaseKnowledgeRepository(KnowledgeRepository, BaseRepository):
    """PostgreSQL-backed knowledge repository using BaseRepository pool access."""

    def __init__(self, pool_holder: Any):
        BaseRepository.__init__(self, pool_holder)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """Convert a database row to a dict, handling JSON and datetime fields."""
        if not row:
            return {}
        result = dict(row)

        json_dict_fields = {"metadata", "embedding_config", "index_config", "result", "config"}
        json_list_fields = {
            "roles",
            "keywords",
            "include_patterns",
            "exclude_patterns",
            "labels",
            "events",
            "history",
        }

        for key, value in result.items():
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif key in json_dict_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = {}
                elif not isinstance(value, dict):
                    result[key] = {}
            elif key in json_list_fields and value is not None:
                if isinstance(value, str):
                    try:
                        result[key] = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        result[key] = []
                elif not isinstance(value, list):
                    result[key] = []
        return result

    # ------------------------------------------------------------------
    # Dataset
    # ------------------------------------------------------------------

    async def save_dataset(self, dataset: dict[str, Any]) -> None:
        """Save or upsert a dataset."""
        if not self.enabled:
            return
        await self.execute(
            """
            INSERT INTO datasets (
                dataset_id, name, description, tenant_id, visibility,
                embedding_provider, embedding_model, embedding_dimension,
                embedding_config, index_config, collection_name,
                is_deleted, deleted_at, deleted_by, delete_reason,
                created_by
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11,
                $12, $13, $14, $15,
                $16
            )
            ON CONFLICT (dataset_id) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                tenant_id = EXCLUDED.tenant_id,
                visibility = EXCLUDED.visibility,
                embedding_provider = EXCLUDED.embedding_provider,
                embedding_model = EXCLUDED.embedding_model,
                embedding_dimension = EXCLUDED.embedding_dimension,
                embedding_config = EXCLUDED.embedding_config,
                index_config = EXCLUDED.index_config,
                collection_name = EXCLUDED.collection_name,
                is_deleted = FALSE,
                deleted_at = NULL,
                deleted_by = NULL,
                delete_reason = NULL,
                created_by = EXCLUDED.created_by,
                updated_at = NOW()
            """,
            dataset.get("dataset_id"),
            dataset.get("name"),
            dataset.get("description"),
            dataset.get("tenant_id", ""),
            dataset.get("visibility", "private"),
            dataset.get("embedding_provider", "gemini"),
            dataset.get("embedding_model", "gemini-embedding-001"),
            int(dataset.get("embedding_dimension") or 0) or 1024,
            json.dumps(dataset.get("embedding_config", {})),
            json.dumps(dataset.get("index_config", {})),
            dataset.get("collection_name"),
            bool(dataset.get("is_deleted", False)),
            dataset.get("deleted_at"),
            dataset.get("deleted_by"),
            dataset.get("delete_reason"),
            dataset.get("created_by"),
        )

    async def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        """Get a single dataset by ID (excludes soft-deleted)."""
        row = await self.fetchrow(
            "SELECT * FROM datasets WHERE dataset_id = $1 AND is_deleted = FALSE", dataset_id
        )
        return self._row_to_dict(row) if row else None

    async def list_datasets(
        self,
        tenant_id: str | None = None,
        include_public: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List datasets with optional tenant/visibility filtering."""
        if not self.enabled:
            return []

        query = "SELECT * FROM datasets WHERE is_deleted = FALSE"
        params: list[Any] = []
        param_idx = 1

        if tenant_id:
            if include_public:
                query += f" AND (tenant_id = ${param_idx} OR visibility = 'public')"
                params.append(tenant_id)
                param_idx += 1
            else:
                query += f" AND tenant_id = ${param_idx}"
                params.append(tenant_id)
                param_idx += 1
        else:
            if not include_public:
                query += " AND visibility != 'public'"

        query += f" ORDER BY created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        rows = await self.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def delete_dataset(
        self,
        dataset_id: str,
        deleted_by: str | None = None,
        delete_reason: str | None = None,
    ) -> bool:
        """Soft-delete a dataset and clean up associated data."""
        if not self.enabled:
            return False
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow(
                    """
                    SELECT dataset_id
                    FROM datasets
                    WHERE dataset_id = $1
                      AND is_deleted = FALSE
                    FOR UPDATE
                    """,
                    dataset_id,
                )
                if not target:
                    return False

                await conn.execute(
                    """
                    UPDATE datasets
                    SET is_deleted = TRUE,
                        deleted_at = NOW(),
                        deleted_by = $2,
                        delete_reason = $3,
                        updated_at = NOW()
                    WHERE dataset_id = $1
                    """,
                    dataset_id,
                    deleted_by,
                    delete_reason,
                )

                # Keep dataset record for audit/compliance, remove active payload data.
                await conn.execute(
                    "DELETE FROM confluence_space_bindings WHERE dataset_id = $1", dataset_id
                )
                await conn.execute(
                    "DELETE FROM version_retention_policies WHERE dataset_id = $1", dataset_id
                )
                await conn.execute(
                    "DELETE FROM dataset_keyword_tables WHERE dataset_id = $1", dataset_id
                )
                await conn.execute(
                    "DELETE FROM dataset_process_rules WHERE dataset_id = $1", dataset_id
                )
                await conn.execute("DELETE FROM dataset_queries WHERE dataset_id = $1", dataset_id)
                await conn.execute("DELETE FROM child_chunks WHERE dataset_id = $1", dataset_id)
                await conn.execute(
                    "DELETE FROM dataset_permissions WHERE dataset_id = $1", dataset_id
                )
                await conn.execute("DELETE FROM documents WHERE dataset_id = $1", dataset_id)

            return True

    async def clear_dataset_needs_reindex(self, dataset_id: str) -> None:
        """Clear the needs_reindex flag after successful document reindexing."""
        await self.execute(
            """
            UPDATE datasets
            SET needs_reindex = false, updated_at = NOW()
            WHERE dataset_id = $1
            """,
            dataset_id,
        )
        logger.info(f"Cleared needs_reindex flag for dataset {dataset_id}")

    async def get_datasets_statistics_batch(
        self, dataset_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """Get document/segment counts for multiple datasets in one query."""
        if not self.enabled or not dataset_ids:
            return {}

        rows = await self.fetch(
            """
            SELECT
                d.dataset_id,
                COUNT(DISTINCT doc.document_id) as document_count,
                COUNT(DISTINCT seg.segment_id) as segment_count
            FROM datasets d
            LEFT JOIN documents doc ON d.dataset_id = doc.dataset_id
            LEFT JOIN segments seg ON d.dataset_id = seg.dataset_id
            WHERE d.dataset_id = ANY($1)
              AND d.is_deleted = FALSE
            GROUP BY d.dataset_id
            """,
            dataset_ids,
        )

        result: dict[str, dict[str, int]] = {}
        for row in rows:
            result[row["dataset_id"]] = {
                "document_count": row["document_count"] or 0,
                "segment_count": row["segment_count"] or 0,
            }

        # Ensure all requested dataset_ids have entries (even if empty)
        for ds_id in dataset_ids:
            if ds_id not in result:
                result[ds_id] = {"document_count": 0, "segment_count": 0}

        return result

    async def dataset_has_embeddings(self, dataset_id: str) -> bool:
        """Check if dataset has any embedded segments with vectors."""
        if not self.enabled:
            return False
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM segments
                WHERE dataset_id = $1 AND vector_id IS NOT NULL
                LIMIT 1
                """,
                dataset_id,
            )
            return (count or 0) > 0

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def grant_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str, permission: str
    ) -> None:
        """Grant a permission on a dataset to a user or role."""
        await self.execute(
            """
            INSERT INTO dataset_permissions (
                dataset_id, subject_type, subject_id, permission
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (dataset_id, subject_type, subject_id) DO UPDATE SET
                permission = EXCLUDED.permission,
                updated_at = NOW()
            """,
            dataset_id,
            subject_type,
            subject_id,
            permission,
        )

    async def revoke_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        """Revoke a dataset permission. Returns True if a row was deleted."""
        result = await self.execute(
            """
            DELETE FROM dataset_permissions
            WHERE dataset_id = $1 AND subject_type = $2 AND subject_id = $3
            """,
            dataset_id,
            subject_type,
            subject_id,
        )
        if result.startswith("DELETE "):
            return int(result.split()[-1]) > 0
        return False

    async def list_dataset_permissions(self, dataset_id: str) -> list[dict[str, Any]]:
        """List all permission entries for a dataset."""
        rows = await self.fetch(
            """
            SELECT * FROM dataset_permissions
            WHERE dataset_id = $1
            ORDER BY created_at ASC
            """,
            dataset_id,
        )
        return [self._row_to_dict(row) for row in rows]

    async def get_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> dict[str, Any] | None:
        """Get a specific permission record for a dataset + subject."""
        row = await self.fetchrow(
            """
            SELECT * FROM dataset_permissions
            WHERE dataset_id = $1 AND subject_type = $2 AND subject_id = $3
            """,
            dataset_id,
            subject_type,
            subject_id,
        )
        return self._row_to_dict(row) if row else None

    # ------------------------------------------------------------------
    # Document  (still delegated -- to be migrated separately)
    # ------------------------------------------------------------------

    async def save_document(self, document: dict[str, Any]) -> None:
        raise NotImplementedError("Document methods not yet migrated")

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("Document methods not yet migrated")

    async def list_documents(
        self,
        dataset_id: str,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Document methods not yet migrated")

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: float | None = None,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError("Document methods not yet migrated")

    async def delete_document(self, document_id: str) -> bool:
        raise NotImplementedError("Document methods not yet migrated")

    # ------------------------------------------------------------------
    # Segment  (still delegated -- to be migrated separately)
    # ------------------------------------------------------------------

    async def insert_segments(self, segments: list[dict[str, Any]]) -> None:
        raise NotImplementedError("Segment methods not yet migrated")

    async def delete_segments_by_document(self, document_id: str) -> int:
        raise NotImplementedError("Segment methods not yet migrated")

    async def list_segments(
        self,
        dataset_id: str,
        document_id: str | None = None,
        query_text: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("Segment methods not yet migrated")

    async def get_segment(self, segment_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("Segment methods not yet migrated")

    async def update_segment(
        self,
        segment_id: str,
        text: str,
        token_count: int | None = None,
        metadata: dict[str, Any] | None = None,
        vector_id: str | None = None,
    ) -> None:
        raise NotImplementedError("Segment methods not yet migrated")

    async def delete_segment(self, segment_id: str) -> bool:
        raise NotImplementedError("Segment methods not yet migrated")
