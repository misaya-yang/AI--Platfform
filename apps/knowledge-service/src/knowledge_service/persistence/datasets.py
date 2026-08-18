"""Dataset persistence helpers and the dataset-facing storage mixin."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from typing import Any

try:
    import asyncpg

    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False
    asyncpg = None

# Preserve the logger category used before this code was extracted.
logger = logging.getLogger("knowledge_service.persistence.database")

INDEX_DELETION_FENCE_KEY = "_index_deletion_fence"
INDEX_DELETION_FENCE_VERSION = 1
DOCUMENT_LIFECYCLE_REINDEX_KEY = "_document_lifecycle_reindex"
DOCUMENT_UPLOAD_GENERATION_KEY = "_document_upload_generation"
DOCUMENT_UPLOAD_FAILED_KEY = "_document_upload_failed"
CONFLUENCE_SYNC_GENERATION_KEY = "_confluence_sync_generation"

_INDEX_DELETION_OPERATIONS = frozenset({"dataset_delete", "document_delete", "segment_delete"})


class IndexLeaseUnavailableError(RuntimeError):
    """A short-lived index lease is busy; callers must defer, not fail work."""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def make_dataset_index_deletion_fence(
    operation: str,
    target_id: str,
) -> dict[str, Any]:
    """Build one deterministic, target-bound deletion fence marker."""

    normalized_operation = str(operation or "").strip()
    normalized_target = str(target_id or "").strip()
    if normalized_operation not in _INDEX_DELETION_OPERATIONS:
        raise ValueError("unsupported dataset index deletion operation")
    if not normalized_target:
        raise ValueError("dataset index deletion target_id is required")
    return {
        "operation": normalized_operation,
        "target_id": normalized_target,
        "status": "pending",
        "version": INDEX_DELETION_FENCE_VERSION,
    }


def dataset_index_deletion_fence(dataset: dict[str, Any]) -> dict[str, Any] | None:
    """Return the validated durable deletion fence, failing closed if malformed."""

    index_config = _json_object(dataset.get("index_config"))
    retrieval = _json_object(index_config.get("retrieval"))
    if INDEX_DELETION_FENCE_KEY not in retrieval:
        return None
    marker = retrieval.get(INDEX_DELETION_FENCE_KEY)
    if not isinstance(marker, dict):
        raise RuntimeError("dataset index deletion fence is malformed")
    operation = marker.get("operation")
    target_id = marker.get("target_id")
    if (
        operation not in _INDEX_DELETION_OPERATIONS
        or not isinstance(target_id, str)
        or not target_id.strip()
        or marker.get("status") != "pending"
        or marker.get("version") != INDEX_DELETION_FENCE_VERSION
    ):
        raise RuntimeError("dataset index deletion fence is malformed")
    return make_dataset_index_deletion_fence(operation, target_id)


def index_config_has_reserved_deletion_fence(index_config: Any) -> bool:
    """Return whether caller-controlled config contains the internal marker key."""

    retrieval = _json_object(_json_object(index_config).get("retrieval"))
    return INDEX_DELETION_FENCE_KEY in retrieval


def dataset_ingestion_identity(dataset: dict[str, Any]) -> str:
    """Hash the dataset choices that determine persisted index generations.

    Retrieval-only tuning is intentionally excluded: it can be changed without
    rebuilding chunks or dense vectors. The hash keeps credential-bearing
    embedding configuration out of logs and exception messages.
    """

    index_config = _json_object(dataset.get("index_config"))
    payload = {
        "tenant_id": str(dataset.get("tenant_id") or ""),
        "collection_name": str(dataset.get("collection_name") or ""),
        "embedding_provider": str(dataset.get("embedding_provider") or ""),
        "embedding_model": str(dataset.get("embedding_model") or ""),
        "embedding_dimension": int(dataset.get("embedding_dimension") or 0),
        "embedding_config": _json_object(dataset.get("embedding_config")),
        "ingestion_index_config": {
            key: value for key, value in index_config.items() if key != "retrieval"
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _dataset_lexical_active_version(dataset: dict[str, Any]) -> str:
    index_config = _json_object(dataset.get("index_config"))
    retrieval = _json_object(index_config.get("retrieval"))
    lexical = _json_object(retrieval.get("lexical"))
    return str(lexical.get("active_version") or "lexical_v1")


class DatasetPersistenceMixin:
    """Dataset CRUD, permissions, ingestion claims, and index leases."""

    async def dataset_exists(self, dataset_id: str) -> bool:
        """Return whether an ID is reserved by any active or soft-deleted dataset."""
        if not self._pool:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM datasets WHERE dataset_id = $1)",
                    dataset_id,
                )
            )

    async def collection_name_in_use(self, collection_name: str) -> bool:
        """Return whether a Qdrant collection name is already bound to a dataset."""
        if not self._pool:
            raise RuntimeError("database is not connected")
        if not collection_name:
            return False
        async with self._pool.acquire() as conn:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM datasets WHERE collection_name = $1)",
                    collection_name,
                )
            )

    async def _insert_dataset_row(self, conn: Any, dataset: dict[str, Any]) -> Any:
        return await conn.fetchrow(
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
            RETURNING dataset_id
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

    async def create_dataset(self, dataset: dict[str, Any]) -> bool:
        """Insert a new dataset without ever overwriting an existing identity."""
        if not self._pool:
            raise RuntimeError("database is not connected")

        try:
            async with self._pool.acquire() as conn:
                row = await self._insert_dataset_row(conn, dataset)
                return row is not None
        except Exception as exc:
            if HAS_ASYNCPG and isinstance(exc, asyncpg.UniqueViolationError):
                return False
            raise

    async def create_dataset_with_owner(
        self,
        dataset: dict[str, Any],
        owner_user_id: str,
    ) -> bool:
        """Atomically insert a dataset and its explicit owner permission."""
        if not self._pool:
            raise RuntimeError("database is not connected")
        if not owner_user_id:
            raise ValueError("owner_user_id is required")

        try:
            async with self._pool.acquire() as conn, conn.transaction():
                row = await self._insert_dataset_row(conn, dataset)
                if row is None:
                    raise RuntimeError("dataset insert returned no identity")
                await conn.execute(
                    """
                    INSERT INTO dataset_permissions (
                        dataset_id, subject_type, subject_id, permission
                    ) VALUES ($1, 'user', $2, 'owner')
                    """,
                    dataset.get("dataset_id"),
                    owner_user_id,
                )
                return True
        except Exception as exc:
            if HAS_ASYNCPG and isinstance(exc, asyncpg.UniqueViolationError):
                return False
            raise

    async def save_dataset(self, dataset: dict[str, Any]) -> None:
        """保存或更新知识库 Dataset"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
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

    async def patch_dataset_fields(
        self,
        dataset_id: str,
        changes: dict[str, Any],
        *,
        expected_config: dict[str, Any] | None = None,
        require_no_documents: bool = False,
    ) -> dict[str, Any] | None:
        """Patch only requested fields, with CAS for retrieval configuration.

        Dataset callers commonly operate on snapshots. Updating the full row
        from such a snapshot can overwrite a concurrent lexical transition,
        so this method deliberately rejects unknown columns and never touches
        fields omitted by the caller. Any embedding/index/collection change
        must match the caller's complete prior configuration projection.
        """
        if not self._pool:
            raise RuntimeError("database is not connected")
        allowed = (
            "name",
            "description",
            "visibility",
            "embedding_provider",
            "embedding_model",
            "embedding_dimension",
            "embedding_config",
            "index_config",
            "collection_name",
        )
        config_fields = {
            "embedding_provider",
            "embedding_model",
            "embedding_dimension",
            "embedding_config",
            "index_config",
            "collection_name",
        }
        unknown = sorted(set(changes) - set(allowed))
        if unknown:
            raise ValueError("unsupported dataset update fields: " + ", ".join(unknown))
        selected = [field for field in allowed if field in changes]
        if not selected:
            return await self.get_dataset(dataset_id)

        expected: dict[str, Any] | None = None
        if config_fields.intersection(selected):
            expected = dict(expected_config or {})
            missing_expected = sorted(config_fields - set(expected))
            if missing_expected:
                raise ValueError(
                    "dataset configuration CAS is missing fields: " + ", ".join(missing_expected)
                )

        values: list[Any] = [dataset_id]
        assignments: list[str] = []
        for field in selected:
            value = changes[field]
            if field in {"embedding_config", "index_config"}:
                value = json.dumps(value or {})
                cast = "::jsonb"
            else:
                cast = ""
            values.append(value)
            assignments.append(f"{field} = ${len(values)}{cast}")

        predicates = ["dataset_id = $1", "is_deleted = FALSE"]
        if expected is not None:
            for field in (
                "embedding_provider",
                "embedding_model",
                "embedding_dimension",
                "embedding_config",
                "index_config",
                "collection_name",
            ):
                value = expected[field]
                if field in {"embedding_config", "index_config"}:
                    value = json.dumps(value or {})
                    cast = "::jsonb"
                else:
                    cast = ""
                values.append(value)
                predicates.append(f"{field} IS NOT DISTINCT FROM ${len(values)}{cast}")
        if require_no_documents:
            predicates.append(
                "NOT EXISTS (SELECT 1 FROM documents "
                "WHERE documents.dataset_id = datasets.dataset_id)"
            )

        async def _patch(conn: Any) -> Any:
            return await conn.fetchrow(
                "UPDATE datasets SET "
                + ", ".join(assignments)
                + ", updated_at = NOW() WHERE "
                + " AND ".join(predicates)
                + " RETURNING *",
                *values,
            )

        async with self._pool.acquire() as conn:
            if require_no_documents:
                async with conn.transaction():
                    await conn.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        self._dataset_index_lock_name(dataset_id),
                    )
                    row = await _patch(conn)
            else:
                row = await _patch(conn)
        return self._row_to_dict(row) if row else None

    async def compare_and_swap_dataset_collection_identity(
        self,
        dataset_id: str,
        *,
        expected_dimension: int,
        expected_collection_name: str,
        replacement_dimension: int,
        replacement_collection_name: str,
    ) -> bool:
        """Initialize dimension/collection without overwriting a newer value."""
        if not self._pool:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.fetchval(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                self._dataset_index_lock_name(dataset_id),
            )
            row = await conn.fetchrow(
                """
                UPDATE datasets
                SET embedding_dimension = $4,
                    collection_name = $5,
                    updated_at = NOW()
                WHERE dataset_id = $1
                  AND is_deleted = FALSE
                  AND COALESCE(embedding_dimension, 0) = $2
                  AND COALESCE(collection_name, '') = $3
                RETURNING dataset_id
                """,
                dataset_id,
                int(expected_dimension or 0),
                str(expected_collection_name or ""),
                int(replacement_dimension),
                replacement_collection_name,
            )
        return row is not None

    @staticmethod
    def _dataset_index_lock_name(dataset_id: str) -> str:
        normalized = str(dataset_id or "").strip()
        if not normalized:
            raise ValueError("dataset_id is required for index locking")
        return f"knowledge-dataset-index:{normalized}"

    def connection_pool_max_size(self) -> int:
        """Expose configured capacity for long-lived worker lease admission."""

        if not self._pool:
            raise RuntimeError("database is not connected")
        getter = getattr(self._pool, "get_max_size", None)
        if not callable(getter):
            raise RuntimeError("database pool does not expose its maximum size")
        return int(getter())

    @staticmethod
    def _segment_index_lock_name(dataset_id: str, segment_id: str) -> str:
        normalized_dataset = str(dataset_id or "").strip()
        normalized_segment = str(segment_id or "").strip()
        if not normalized_dataset or not normalized_segment:
            raise ValueError("dataset_id and segment_id are required for segment locking")
        return f"knowledge-segment-index:{normalized_dataset}:{normalized_segment}"

    @staticmethod
    def _document_index_lock_name(dataset_id: str, document_id: str) -> str:
        normalized_dataset = str(dataset_id or "").strip()
        normalized_document = str(document_id or "").strip()
        if not normalized_dataset or not normalized_document:
            raise ValueError("dataset_id and document_id are required for document locking")
        return f"knowledge-document-index:{normalized_dataset}:{normalized_document}"

    @contextlib.asynccontextmanager
    async def document_index_update_lease(
        self,
        dataset_id: str,
        document_id: str,
    ):
        """Short dataset-shared/document-exclusive cross-replica barrier."""

        if not self._pool:
            raise RuntimeError("database is not connected")
        dataset_lock_name = self._dataset_index_lock_name(dataset_id)
        document_lock_name = self._document_index_lock_name(dataset_id, document_id)
        async with self._pool.acquire() as conn:
            dataset_acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock_shared(hashtextextended($1, 0))",
                dataset_lock_name,
            )
            if dataset_acquired is not True:
                raise IndexLeaseUnavailableError(
                    "dataset index deletion is in progress; refusing document index update"
                )
            document_acquired = False
            try:
                document_acquired = await conn.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    document_lock_name,
                )
                if document_acquired is not True:
                    raise IndexLeaseUnavailableError("document index update is already in progress")
                yield conn
            finally:
                try:
                    if document_acquired:
                        document_unlock = asyncio.create_task(
                            conn.fetchval(
                                "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                                document_lock_name,
                            )
                        )
                        try:
                            document_released = await asyncio.shield(document_unlock)
                        except asyncio.CancelledError:
                            document_released = await document_unlock
                            raise
                        if document_released is not True:
                            raise RuntimeError("document index update lease was not released")
                finally:
                    dataset_unlock = asyncio.create_task(
                        conn.fetchval(
                            "SELECT pg_advisory_unlock_shared(hashtextextended($1, 0))",
                            dataset_lock_name,
                        )
                    )
                    try:
                        dataset_released = await asyncio.shield(dataset_unlock)
                    except asyncio.CancelledError:
                        dataset_released = await dataset_unlock
                        raise
                    if dataset_released is not True:
                        raise RuntimeError("dataset shared index lease was not released")

    @contextlib.asynccontextmanager
    async def segment_index_update_lease(
        self,
        dataset_id: str,
        document_id: str,
        segment_id: str,
    ):
        """Lock dataset, document generation, then one segment generation."""

        segment_lock_name = self._segment_index_lock_name(dataset_id, segment_id)
        async with self.document_index_update_lease(dataset_id, document_id) as conn:
            segment_acquired = False
            try:
                segment_acquired = await conn.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    segment_lock_name,
                )
                if segment_acquired is not True:
                    raise IndexLeaseUnavailableError("segment index update is already in progress")
                yield conn
            finally:
                if segment_acquired:
                    segment_unlock = asyncio.create_task(
                        conn.fetchval(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            segment_lock_name,
                        )
                    )
                    try:
                        segment_released = await asyncio.shield(segment_unlock)
                    except asyncio.CancelledError:
                        segment_released = await segment_unlock
                        raise
                    if segment_released is not True:
                        raise RuntimeError("segment index update lease was not released")

    @contextlib.asynccontextmanager
    async def document_segment_create_lease(
        self,
        dataset_id: str,
        document_id: str,
    ):
        """Fence deletion and serialize position allocation for one document."""

        async with self.document_index_update_lease(dataset_id, document_id) as conn:
            yield conn

    async def claim_document_for_enqueue(
        self,
        dataset_id: str,
        document_id: str,
    ) -> bool:
        """Durably move one eligible document into the queued generation."""

        async with self.document_index_update_lease(dataset_id, document_id) as conn:
            dataset = await self._require_dataset_ingestion_identity(
                conn,
                dataset_id,
                None,
            )
            if _dataset_lexical_active_version(dataset) != "lexical_v1":
                raise RuntimeError("bm25_v2 active mode is read-only; refusing document enqueue")
            row = await conn.fetchrow(
                f"""
                UPDATE documents
                SET status = 'queued',
                    progress = 0,
                    error = NULL,
                    updated_at = NOW()
                WHERE document_id = $1
                  AND dataset_id = $2
                  AND status IN ('uploaded', 'completed', 'failed')
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_GENERATION_KEY}'
                  )
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_FAILED_KEY}'
                  )
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{CONFLUENCE_SYNC_GENERATION_KEY}'
                  )
                  AND (
                        (
                            COALESCE(enabled, TRUE) = TRUE
                            AND COALESCE(archived, FALSE) = FALSE
                            AND NOT (
                                COALESCE(metadata, '{{}}'::jsonb)
                                ? '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                            )
                        )
                        OR metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                            ->> 'status' = 'pending'
                            AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'desired_enabled' = 'true'
                            AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'desired_archived' = 'false'
                  )
                RETURNING document_id
                """,
                document_id,
                dataset_id,
            )
            return row is not None

    async def list_queued_documents(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return bounded durable work for worker-role dispatch.

        This is discovery only. The consumer still performs the authoritative
        queued-to-processing CAS while holding the document owner lease, so
        concurrent worker replicas may observe the same row but cannot process
        the same generation twice.
        """

        if not self._pool:
            return []
        bounded_limit = min(max(int(limit), 1), 1000)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT d.dataset_id, d.document_id
                FROM documents AS d
                JOIN datasets AS ds ON ds.dataset_id = d.dataset_id
                WHERE d.status = 'queued'
                  AND ds.is_deleted = FALSE
                  AND NOT (
                        COALESCE(d.metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_GENERATION_KEY}'
                  )
                  AND NOT (
                        COALESCE(d.metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_FAILED_KEY}'
                  )
                  AND NOT (
                        COALESCE(d.metadata, '{{}}'::jsonb)
                        ? '{CONFLUENCE_SYNC_GENERATION_KEY}'
                  )
                  AND NOT COALESCE(
                        COALESCE(ds.index_config, '{{}}'::jsonb)
                            -> 'retrieval' ? '{INDEX_DELETION_FENCE_KEY}',
                        FALSE
                  )
                  AND COALESCE(
                        ds.index_config -> 'retrieval' -> 'lexical'
                            ->> 'active_version',
                        'lexical_v1'
                  ) = 'lexical_v1'
                ORDER BY d.updated_at ASC, d.document_id ASC
                LIMIT $1
                """,
                bounded_limit,
            )
        return [self._row_to_dict(row) for row in rows]

    async def claim_queued_document_for_processing(
        self,
        dataset_id: str,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """CAS one durable queued row to processing before consuming it."""

        async def _claim(conn: Any) -> bool:
            dataset = await self._require_dataset_ingestion_identity(
                conn,
                dataset_id,
                None,
            )
            if _dataset_lexical_active_version(dataset) != "lexical_v1":
                raise RuntimeError(
                    "bm25_v2 active mode is read-only; refusing document consumer claim"
                )
            row = await conn.fetchrow(
                f"""
                UPDATE documents
                SET status = 'processing',
                    progress = 0,
                    error = NULL,
                    updated_at = NOW(),
                    started_at = COALESCE(started_at, NOW())
                WHERE document_id = $1
                  AND dataset_id = $2
                  AND status = 'queued'
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_GENERATION_KEY}'
                  )
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{DOCUMENT_UPLOAD_FAILED_KEY}'
                  )
                  AND NOT (
                        COALESCE(metadata, '{{}}'::jsonb)
                        ? '{CONFLUENCE_SYNC_GENERATION_KEY}'
                  )
                  AND (
                        (
                            COALESCE(enabled, TRUE) = TRUE
                            AND COALESCE(archived, FALSE) = FALSE
                            AND NOT (
                                COALESCE(metadata, '{{}}'::jsonb)
                                ? '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                            )
                        )
                        OR (
                            metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'status' = 'pending'
                            AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'desired_enabled' = 'true'
                            AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'desired_archived' = 'false'
                        )
                  )
                RETURNING document_id
                """,
                document_id,
                dataset_id,
            )
            return row is not None

        if connection is not None:
            return await _claim(connection)
        async with self.document_index_update_lease(dataset_id, document_id) as conn:
            return await _claim(conn)

    async def requeue_cancelled_document_generation(
        self,
        dataset_id: str,
        document_id: str,
        *,
        connection: Any,
    ) -> bool:
        """Return one owned processing generation to the durable queue."""

        row = await connection.fetchrow(
            """
            UPDATE documents
            SET status = 'queued',
                progress = 0,
                error = NULL,
                updated_at = NOW()
            WHERE document_id = $1
              AND dataset_id = $2
              AND status IN (
                    'processing',
                    'parsing',
                    'segmenting',
                    'embedding',
                    'embedding_images'
              )
            RETURNING document_id
            """,
            document_id,
            dataset_id,
        )
        return row is not None

    async def next_segment_position(
        self,
        dataset_id: str,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> int:
        """Allocate the next position while the caller holds the document lease."""

        if not self._pool:
            raise RuntimeError("database is not connected")

        async def _read(conn: Any) -> int:
            value = await conn.fetchval(
                """
                SELECT COALESCE(MAX(position), -1) + 1
                FROM segments
                WHERE dataset_id = $1 AND document_id = $2
                """,
                dataset_id,
                document_id,
            )
            return int(value or 0)

        if connection is not None:
            return await _read(connection)
        async with self._pool.acquire() as conn:
            return await _read(conn)

    async def _require_dataset_ingestion_identity(
        self,
        conn: Any,
        dataset_id: str,
        expected_ingestion_identity: str | None,
    ) -> dict[str, Any]:
        row = await conn.fetchrow(
            """
            SELECT tenant_id, collection_name,
                   embedding_provider, embedding_model, embedding_dimension,
                   embedding_config, index_config
            FROM datasets
            WHERE dataset_id = $1 AND is_deleted = FALSE
            """,
            dataset_id,
        )
        if not row:
            raise RuntimeError("dataset was deleted before index write; refusing orphan content")
        if dataset_index_deletion_fence(dict(row)) is not None:
            raise RuntimeError("dataset index deletion is pending; refusing indexed-content access")
        if (
            expected_ingestion_identity is not None
            and dataset_ingestion_identity(dict(row)) != expected_ingestion_identity
        ):
            raise RuntimeError(
                "dataset ingestion identity changed; refusing a mixed index generation"
            )
        return dict(row)

    @contextlib.asynccontextmanager
    async def dataset_index_write_lease(
        self,
        dataset_id: str,
        document_ids: list[str],
        *,
        expected_ingestion_identity: str | None = None,
    ):
        """Fence one Qdrant upsert against dataset/document deletion.

        The transaction-scoped shared advisory lock is held across the remote
        upsert. A writer that encounters an in-progress exclusive deletion
        fails closed instead of waiting until the deletion has cleared its
        marker and then recreating a point that was just removed.
        """
        if not self._pool:
            raise RuntimeError("database is not connected")
        lock_name = self._dataset_index_lock_name(dataset_id)
        normalized_ids = sorted(
            {str(document_id).strip() for document_id in document_ids if str(document_id).strip()}
        )
        async with self._pool.acquire() as conn, conn.transaction():
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock_shared(hashtextextended($1, 0))",
                lock_name,
            )
            if acquired is not True:
                raise RuntimeError(
                    "dataset index deletion is in progress; refusing a queued vector write"
                )
            await self._require_dataset_ingestion_identity(
                conn,
                dataset_id,
                expected_ingestion_identity,
            )
            if normalized_ids:
                count = await conn.fetchval(
                    f"""
                    SELECT COUNT(*)
                    FROM documents
                    WHERE dataset_id = $1
                      AND document_id = ANY($2::text[])
                      AND NOT (
                            COALESCE(metadata, '{{}}'::jsonb)
                            ? '{DOCUMENT_UPLOAD_GENERATION_KEY}'
                      )
                      AND NOT (
                            COALESCE(metadata, '{{}}'::jsonb)
                            ? '{DOCUMENT_UPLOAD_FAILED_KEY}'
                      )
                      AND NOT (
                            COALESCE(metadata, '{{}}'::jsonb)
                            ? '{CONFLUENCE_SYNC_GENERATION_KEY}'
                      )
                      AND (
                            (
                                COALESCE(enabled, TRUE) = TRUE
                                AND COALESCE(archived, FALSE) = FALSE
                                AND NOT (
                                    COALESCE(metadata, '{{}}'::jsonb)
                                    ? '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                )
                            )
                            OR metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                ->> 'status' = 'pending'
                                AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                    ->> 'desired_enabled' = 'true'
                                AND metadata -> '{DOCUMENT_LIFECYCLE_REINDEX_KEY}'
                                    ->> 'desired_archived' = 'false'
                      )
                    """,
                    dataset_id,
                    normalized_ids,
                )
                if int(count or 0) != len(normalized_ids):
                    raise RuntimeError(
                        "document is missing or inactive before vector write; "
                        "refusing orphan or disabled points"
                    )
            yield

    @contextlib.asynccontextmanager
    async def dataset_index_delete_lease(self, dataset_id: str):
        """Exclude centrally routed index writes/schema mutation during deletion.

        This is deliberately a session advisory lock, rather than a
        transaction-scoped lock. The yielded connection can commit the durable
        deletion marker before the first remote mutation while retaining the
        exclusive barrier until the caller finishes or fails.
        """
        if not self._pool:
            raise RuntimeError("database is not connected")
        lock_name = self._dataset_index_lock_name(dataset_id)
        async with self._pool.acquire() as conn:
            acquired = False
            try:
                acquired = await conn.fetchval(
                    "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                    lock_name,
                )
                if acquired is not True:
                    raise IndexLeaseUnavailableError(
                        "dataset index lifecycle work is already in progress"
                    )
                yield conn
            finally:
                if acquired:
                    unlock_task = asyncio.create_task(
                        conn.fetchval(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            lock_name,
                        )
                    )
                    try:
                        unlocked = await asyncio.shield(unlock_task)
                    except asyncio.CancelledError:
                        unlocked = await unlock_task
                        raise
                    if unlocked is not True:
                        raise RuntimeError("dataset index deletion lease was not released")

    async def set_dataset_index_deletion_fence(
        self,
        dataset_id: str,
        *,
        operation: str,
        target_id: str,
        connection: Any,
    ) -> tuple[dict[str, Any], bool]:
        """CAS-set a durable deletion marker on the exclusive lease connection.

        An exact same-target retry reuses the existing marker. A different or
        malformed marker is never overwritten.
        """

        if connection is None:
            raise RuntimeError("exclusive dataset deletion lease connection is required")
        marker = make_dataset_index_deletion_fence(operation, target_id)
        marker_json = json.dumps(marker, separators=(",", ":"), sort_keys=True)
        row = await connection.fetchrow(
            f"""
            UPDATE datasets
            SET index_config = jsonb_set(
                    COALESCE(index_config, '{{}}'::jsonb),
                    '{{retrieval}}',
                    CASE
                        WHEN jsonb_typeof(index_config->'retrieval') = 'object'
                        THEN index_config->'retrieval'
                        ELSE '{{}}'::jsonb
                    END
                        || jsonb_build_object(
                            '{INDEX_DELETION_FENCE_KEY}', $2::jsonb
                        ),
                    TRUE
                ),
                content_revision = COALESCE(content_revision, 0) + 1,
                updated_at = NOW()
            WHERE dataset_id = $1
              AND is_deleted = FALSE
              AND jsonb_typeof(COALESCE(index_config, '{{}}'::jsonb)) = 'object'
              AND NOT (
                  CASE
                      WHEN jsonb_typeof(index_config->'retrieval') = 'object'
                      THEN index_config->'retrieval'
                      ELSE '{{}}'::jsonb
                  END
                  ? '{INDEX_DELETION_FENCE_KEY}'
              )
            RETURNING *
            """,
            dataset_id,
            marker_json,
        )
        if row:
            created_dataset = self._row_to_dict(row)
            if dataset_index_deletion_fence(created_dataset) != marker:
                raise RuntimeError("dataset index deletion fence CAS verification failed")
            return created_dataset, True

        current = await connection.fetchrow(
            "SELECT * FROM datasets WHERE dataset_id = $1 AND is_deleted = FALSE",
            dataset_id,
        )
        if not current:
            raise RuntimeError("dataset was deleted before index deletion")
        current_dataset = self._row_to_dict(current)
        existing = dataset_index_deletion_fence(current_dataset)
        if existing != marker:
            raise RuntimeError("another dataset index deletion target is already pending")
        return current_dataset, False

    async def clear_dataset_index_deletion_fence(
        self,
        dataset_id: str,
        *,
        operation: str,
        target_id: str,
        connection: Any,
    ) -> bool:
        """Clear only the exact marker owned by the successful delete target."""

        if connection is None:
            raise RuntimeError("exclusive dataset deletion lease connection is required")
        marker = make_dataset_index_deletion_fence(operation, target_id)
        marker_json = json.dumps(marker, separators=(",", ":"), sort_keys=True)
        row = await connection.fetchrow(
            f"""
            UPDATE datasets
            SET index_config = jsonb_set(
                    COALESCE(index_config, '{{}}'::jsonb),
                    '{{retrieval}}',
                    CASE
                        WHEN jsonb_typeof(index_config->'retrieval') = 'object'
                        THEN index_config->'retrieval'
                        ELSE '{{}}'::jsonb
                    END
                        - '{INDEX_DELETION_FENCE_KEY}',
                    TRUE
                ),
                content_revision = COALESCE(content_revision, 0) + 1,
                updated_at = NOW()
            WHERE dataset_id = $1
              AND is_deleted = FALSE
              AND jsonb_typeof(COALESCE(index_config, '{{}}'::jsonb)) = 'object'
              AND CASE
                      WHEN jsonb_typeof(index_config->'retrieval') = 'object'
                      THEN index_config->'retrieval'
                      ELSE '{{}}'::jsonb
                  END
                    ->'{INDEX_DELETION_FENCE_KEY}' = $2::jsonb
            RETURNING dataset_id
            """,
            dataset_id,
            marker_json,
        )
        return row is not None

    async def clear_dataset_needs_reindex(self, dataset_id: str) -> None:
        """Clear the needs_reindex flag after successful document reindexing."""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE datasets
                SET needs_reindex = false,
                    content_revision = content_revision + 1,
                    updated_at = NOW()
                WHERE dataset_id = $1
                """,
                dataset_id,
            )
            logger.info(f"Cleared needs_reindex flag for dataset {dataset_id}")

    async def get_dataset(
        self,
        dataset_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        """获取 Dataset"""
        if not self._pool:
            return None
        if connection is not None:
            row = await connection.fetchrow(
                "SELECT * FROM datasets WHERE dataset_id = $1 AND is_deleted = FALSE",
                dataset_id,
            )
            return self._row_to_dict(row) if row else None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
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
        """列出 Dataset"""
        if not self._pool:
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

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [self._row_to_dict(row) for row in rows]

    async def delete_dataset(
        self,
        dataset_id: str,
        deleted_by: str | None = None,
        delete_reason: str | None = None,
        *,
        connection: Any | None = None,
    ) -> bool:
        """软删除 Dataset，并清理关联数据。"""
        if not self._pool:
            return False

        async def _delete(conn: Any) -> bool:
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

        if connection is not None:
            return await _delete(connection)
        async with self._pool.acquire() as conn:
            return await _delete(conn)

    async def get_datasets_statistics_batch(
        self, dataset_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """获取多个 Dataset 的统计数据（批量查询优化）"""
        if not self._pool or not dataset_ids:
            return {}

        async with self._pool.acquire() as conn:
            # Two lightweight sub-selects instead of double LEFT JOIN (avoids cartesian on 52K segments)
            query = """
                SELECT
                    d.dataset_id,
                    COALESCE((SELECT COUNT(*) FROM documents doc WHERE doc.dataset_id = d.dataset_id), 0) as document_count,
                    COALESCE((SELECT COUNT(*) FROM segments seg WHERE seg.dataset_id = d.dataset_id), 0) as segment_count
                FROM datasets d
                WHERE d.dataset_id = ANY($1)
                  AND d.is_deleted = FALSE
            """
            rows = await conn.fetch(query, dataset_ids)

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

    async def grant_dataset_permission(
        self,
        dataset_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
    ) -> None:
        """授予 Dataset 权限（subject_type=user|role）"""
        if not self._pool:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
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
        """撤销 Dataset 权限"""
        if not self._pool:
            return False
        async with self._pool.acquire() as conn:
            result = await conn.execute(
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
        """列出 Dataset 权限"""
        if not self._pool:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
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
        """获取指定 subject 对 Dataset 的权限记录"""
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM dataset_permissions
                WHERE dataset_id = $1 AND subject_type = $2 AND subject_id = $3
                """,
                dataset_id,
                subject_type,
                subject_id,
            )
            return self._row_to_dict(row) if row else None
