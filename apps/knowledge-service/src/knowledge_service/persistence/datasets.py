"""Dataset persistence helpers and the dataset-facing storage mixin."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
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

# PRD T1 items 3/4 + addendum §1-T1: dual-verb reprocessing contract.
# The durable queue is the set of documents.status='waiting' rows; the verb a
# queued generation must run is pinned on the row at claim time so it cannot
# drift between enqueue and dispatch. 'ingest' is the default first-generation
# pipeline and carries no marker. The marker vocabulary mirrors the action
# column of document_pipeline_executions (migration 101).
DOCUMENT_INGEST_ACTION_KEY = "_document_ingest_action"
DOCUMENT_RECOVER_STAGE_KEY = "_document_recover_stage"
DOCUMENT_PIPELINE_EXECUTION_KEY = "_document_pipeline_execution_id"
INGEST_ACTION_VOCABULARY = frozenset(
    {"ingest", "reprocess", "reembed", "recover", "retry"}
)
# Interactive single-document verbs jump the durable queue ahead of bulk
# import backlogs (PRD §3.1 dual queue, adapt-3: routing by operation type).
PRIORITY_INGEST_ACTIONS = frozenset({"reprocess", "reembed", "recover", "retry"})
# Interactive work may overtake bulk work queued at most this much earlier.
# The finite bias preserves low-latency repairs without letting a continuous
# stream of repairs starve an older bulk generation forever.
INTERACTIVE_QUEUE_BIAS_SECONDS = 5 * 60
# Stuck stages the crash-recovery loop can observe; they decide the recover
# branch (PRD T1 item 4: splitting redoes the full pipeline, indexing rebuilds
# vectors only from already-persisted chunks).
RECOVER_STAGE_VOCABULARY = frozenset({"parsing", "splitting", "indexing"})

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


NON_INGESTION_INDEX_CONFIG_KEYS = frozenset(
    {"retrieval", "document_metadata_registry"}
)


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
            key: value
            for key, value in index_config.items()
            if key not in NON_INGESTION_INDEX_CONFIG_KEYS
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        *,
        action: str | None = None,
        recover_stage: str | None = None,
        execution_id: str | None = None,
    ) -> bool:
        """Durably move one eligible document into the queued generation.

        ``action`` pins the dual-verb contract (PRD T1 item 3) on the queued
        row atomically with the waiting-transition; ``None`` means the default
        first-generation pipeline and clears any stale verb left by an earlier
        generation. ``recover_stage`` records the stage a crashed generation
        died in (recover verb only); ``execution_id`` links the queued row to
        its document_pipeline_executions replay snapshot (addendum §1-T1.3).
        """

        normalized_action = str(action or "").strip().lower() or None
        if normalized_action is not None and normalized_action not in INGEST_ACTION_VOCABULARY:
            raise ValueError(f"unsupported ingest action: {action}")
        normalized_stage = str(recover_stage or "").strip().lower() or None
        if normalized_stage is not None and normalized_stage not in RECOVER_STAGE_VOCABULARY:
            raise ValueError(f"unsupported recover stage: {recover_stage}")
        if normalized_stage and normalized_action != "recover":
            raise ValueError("recover_stage is only valid with the recover action")
        normalized_execution_id = str(execution_id or "").strip() or None

        # Build the exact metadata patch in Python; the UPDATE merges it over
        # the authoritative row after stripping stage/exec keys so a verb can
        # never inherit stale replay state from a previous generation.
        if normalized_action is None or normalized_action == "ingest":
            metadata_patch: dict[str, Any] | None = None
        else:
            metadata_patch = {DOCUMENT_INGEST_ACTION_KEY: normalized_action}
            if normalized_stage:
                metadata_patch[DOCUMENT_RECOVER_STAGE_KEY] = normalized_stage
            if normalized_execution_id:
                metadata_patch[DOCUMENT_PIPELINE_EXECUTION_KEY] = normalized_execution_id

        async with self.document_index_update_lease(dataset_id, document_id) as conn:
            await self._require_dataset_ingestion_identity(
                conn,
                dataset_id,
                None,
            )
            row = await conn.fetchrow(
                f"""
                UPDATE documents
                SET status = 'waiting',
                    progress = 0,
                    error = NULL,
                    -- This claim opens a new pipeline generation. Stage
                    -- timestamps describe that generation only; retaining a
                    -- prior run makes the UI report ever-growing parsing /
                    -- splitting durations. Cancellation/recovery requeues use
                    -- requeue_cancelled_document_generation instead and keep
                    -- their current-generation stamps.
                    started_at = NULL,
                    completed_at = NULL,
                    parsing_started_at = NULL,
                    splitting_started_at = NULL,
                    indexing_started_at = NULL,
                    updated_at = NOW(),
                    metadata = CASE
                        WHEN $3::jsonb IS NULL THEN
                            COALESCE(metadata, '{{}}'::jsonb)
                                - '{DOCUMENT_INGEST_ACTION_KEY}'
                                - '{DOCUMENT_RECOVER_STAGE_KEY}'
                                - '{DOCUMENT_PIPELINE_EXECUTION_KEY}'
                        ELSE
                            (COALESCE(metadata, '{{}}'::jsonb)
                                - '{DOCUMENT_RECOVER_STAGE_KEY}'
                                - '{DOCUMENT_PIPELINE_EXECUTION_KEY}')
                            || $3::jsonb
                    END
                WHERE document_id = $1
                  AND dataset_id = $2
                  AND status IN ('waiting', 'completed', 'error')
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
                  -- A row already queued under a verb belongs to that verb:
                  -- only an identical re-claim may re-pin it. A different
                  -- verb (or a plain claim that would strip the marker) is
                  -- rejected so a queued generation can never be silently
                  -- swapped or demoted. Terminal rows are exempt: they never
                  -- legitimately carry a marker, and re-claiming is the
                  -- self-healing path for stale ones.
                  AND (
                        status <> 'waiting'
                        OR NOT (
                            COALESCE(metadata, '{{}}'::jsonb)
                            ? '{DOCUMENT_INGEST_ACTION_KEY}'
                        )
                        OR COALESCE(metadata ->> '{DOCUMENT_INGEST_ACTION_KEY}', '')
                            = $4
                  )
                RETURNING document_id
                """,
                document_id,
                dataset_id,
                json.dumps(metadata_patch) if metadata_patch is not None else None,
                normalized_action or "",
            )
            return row is not None

    async def pin_document_ingest_action(
        self,
        dataset_id: str,
        document_id: str,
        action: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """Pin the dual-verb marker on a lifecycle-owned restore generation.

        Restore transitions persist ``status='waiting'`` directly under the
        dataset-exclusive lifecycle lease, whose exclusive advisory lock makes
        the shared lock taken by claim_document_for_enqueue unacquirable, so
        the verb marker needs this owner-scoped writer. Like the claim path,
        the write strips stale replay state first so the verb never inherits
        a previous generation's stage/exec keys. Generic callers cannot reach
        the verb keys through update_document_fields.
        """

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in INGEST_ACTION_VOCABULARY:
            raise ValueError(f"unsupported ingest action: {action}")
        query = f"""
            UPDATE documents
            SET metadata = (
                    (COALESCE(metadata, '{{}}'::jsonb)
                        - '{DOCUMENT_RECOVER_STAGE_KEY}'
                        - '{DOCUMENT_PIPELINE_EXECUTION_KEY}')
                    || jsonb_build_object('{DOCUMENT_INGEST_ACTION_KEY}', $3::text)
                ),
                updated_at = NOW()
            WHERE document_id = $1 AND dataset_id = $2
            RETURNING document_id
        """
        if connection is not None:
            row = await connection.fetchrow(
                query, document_id, dataset_id, normalized_action
            )
            return row is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query, document_id, dataset_id, normalized_action
            )
            return row is not None

    async def list_queued_documents(
        self,
        *,
        limit: int = 100,
        tenant_cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded durable work for worker-role dispatch.

        This is discovery only. The consumer still performs the authoritative
        queued-to-processing CAS while holding the document owner lease, so
        concurrent worker replicas may observe the same row but cannot process
        the same generation twice. Rows are round-robin by tenant; within each
        tenant, interactive verbs receive a finite age bias over bulk work.
        ``tenant_cursor`` rotates the first tenant across polling batches.
        """

        if not self._pool:
            return []
        bounded_limit = min(max(int(limit), 1), 1000)
        normalized_cursor = str(tenant_cursor or "").strip()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                WITH eligible AS (
                    SELECT
                        ds.tenant_id,
                        d.dataset_id,
                        d.document_id,
                        d.updated_at,
                        CASE
                            WHEN COALESCE(d.metadata, '{{}}'::jsonb)
                                ->> '{DOCUMENT_INGEST_ACTION_KEY}'
                                = ANY($4::text[])
                            THEN 'interactive'
                            ELSE 'bulk'
                        END AS dispatch_lane,
                        d.updated_at - (
                            CASE
                                WHEN COALESCE(d.metadata, '{{}}'::jsonb)
                                    ->> '{DOCUMENT_INGEST_ACTION_KEY}'
                                    = ANY($4::text[])
                                THEN $3::double precision
                                ELSE 0
                            END * INTERVAL '1 second'
                        ) AS dispatch_order_at
                    FROM documents AS d
                    JOIN datasets AS ds ON ds.dataset_id = d.dataset_id
                    WHERE d.status = 'waiting'
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
                ), tenant_ranked AS (
                    SELECT
                        eligible.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY tenant_id
                            ORDER BY dispatch_order_at, updated_at, document_id
                        ) AS tenant_round
                    FROM eligible
                )
                SELECT
                    tenant_id,
                    dataset_id,
                    document_id,
                    dispatch_lane
                FROM tenant_ranked
                ORDER BY
                    tenant_round,
                    CASE
                        WHEN $2::text = '' OR tenant_id > $2::text THEN 0
                        ELSE 1
                    END,
                    tenant_id,
                    dispatch_order_at,
                    updated_at,
                    document_id
                LIMIT $1
                """,
                bounded_limit,
                normalized_cursor,
                float(INTERACTIVE_QUEUE_BIAS_SECONDS),
                sorted(PRIORITY_INGEST_ACTIONS),
            )
        return [self._row_to_dict(row) for row in rows]

    async def count_queued_documents(self) -> int:
        """Count the same dispatchable rows ``list_queued_documents`` serves.

        Feeds the ``kb_ingestion_queue_depth`` gauge at ``/metrics`` scrape
        time. The WHERE clause must stay in lockstep with
        ``list_queued_documents``: a depth that disagrees with what workers
        can actually claim misleads capacity alerts.
        """

        if not self._pool:
            return 0
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT count(*) AS depth
                FROM documents AS d
                JOIN datasets AS ds ON ds.dataset_id = d.dataset_id
                WHERE d.status = 'waiting'
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
                """
            )
        if row is None:
            return 0
        return int(row["depth"] or 0)

    async def claim_queued_document_for_processing(
        self,
        dataset_id: str,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """CAS one durable queued row to processing before consuming it."""

        async def _claim(conn: Any) -> bool:
            await self._require_dataset_ingestion_identity(
                conn,
                dataset_id,
                None,
            )
            row = await conn.fetchrow(
                f"""
                UPDATE documents
                SET status = 'parsing',
                    progress = 0,
                    error = NULL,
                    updated_at = NOW(),
                    started_at = COALESCE(started_at, NOW()),
                    parsing_started_at = COALESCE(parsing_started_at, NOW())
                WHERE document_id = $1
                  AND dataset_id = $2
                  AND status = 'waiting'
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
            SET status = 'waiting',
                progress = 0,
                error = NULL,
                updated_at = NOW()
            WHERE document_id = $1
              AND dataset_id = $2
              AND status IN (
                    'parsing',
                    'splitting',
                    'indexing'
              )
            RETURNING document_id
            """,
            document_id,
            dataset_id,
        )
        return row is not None

    # ------------------------------------------------------------------
    # Document progress events (migration 111 / H1 #4).  The event ledger is
    # append-only and read-only from this API; the database trigger records all
    # progress writes so API and worker processes share one replay cursor.
    # ------------------------------------------------------------------

    async def list_document_progress_events(
        self,
        dataset_id: str,
        *,
        after_sequence: int = 0,
        document_ids: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return dataset-scoped progress events after a durable cursor."""

        if not self._pool:
            raise RuntimeError("database is not connected")
        normalized_dataset = str(dataset_id or "").strip()
        if not normalized_dataset:
            raise ValueError("dataset_id is required")
        try:
            cursor = max(int(after_sequence), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("after_sequence must be a non-negative integer") from exc
        bounded_limit = min(max(int(limit), 1), 500)
        normalized_documents = sorted(
            {
                str(document_id or "").strip()
                for document_id in (document_ids or [])
                if str(document_id or "").strip()
            }
        ) or None

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_sequence, dataset_id, document_id, event_type,
                       payload, created_at
                FROM kb_document_progress_events
                WHERE dataset_id = $1
                  AND event_sequence > $2
                  AND ($3::varchar[] IS NULL OR document_id = ANY($3::varchar[]))
                ORDER BY event_sequence ASC
                LIMIT $4
                """,
                normalized_dataset,
                cursor,
                normalized_documents,
                bounded_limit,
            )
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Per-document pipeline executions (migration 101). One row per queued
    # generation: the immutable input snapshot reprocess/recover replay
    # (addendum §1-T1.3 — in-flight documents must not drift to a config
    # changed after submission) plus the staging manifest for the revision
    # flip (PRD T1.5).
    # ------------------------------------------------------------------

    async def record_pipeline_execution(
        self,
        document_id: str,
        dataset_id: str,
        *,
        action: str,
        trigger_source: str = "api",
        triggered_by: str | None = None,
        process_rule_id: str | None = None,
        input_snapshot: dict[str, Any] | None = None,
        connection: Any | None = None,
    ) -> str:
        """Insert one execution row at submission time; returns its id."""

        if not self._pool:
            raise RuntimeError("database is not connected")
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in INGEST_ACTION_VOCABULARY:
            raise ValueError(f"unsupported pipeline execution action: {action}")
        normalized_trigger = str(trigger_source or "").strip().lower() or "api"
        if normalized_trigger not in {
            "upload",
            "api",
            "worker",
            "confluence_sync",
            "recover",
        }:
            raise ValueError(f"unsupported pipeline execution trigger: {trigger_source}")
        snapshot = input_snapshot if isinstance(input_snapshot, dict) else {}
        execution_id = uuid.uuid4().hex

        async def _insert(conn: Any) -> None:
            await conn.execute(
                """
                INSERT INTO document_pipeline_executions (
                    execution_id,
                    document_id,
                    dataset_id,
                    action,
                    trigger_source,
                    triggered_by,
                    process_rule_id,
                    input_snapshot,
                    manifest,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, '{}'::jsonb, 'running')
                """,
                execution_id,
                document_id,
                dataset_id,
                normalized_action,
                normalized_trigger,
                str(triggered_by or "").strip() or None,
                str(process_rule_id or "").strip() or None,
                json.dumps(snapshot),
            )

        if connection is not None:
            await _insert(connection)
            return execution_id
        async with self._pool.acquire() as conn:
            await _insert(conn)
        return execution_id

    async def link_pipeline_execution(
        self,
        document_id: str,
        execution_id: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """Persist the queued-generation -> execution link on the document row.

        Internal writer only (the key is reserved against API callers). Keeping
        the link in metadata lets a requeued generation (cancel/requeue, crash
        recovery) find its execution row instead of opening a duplicate.
        """

        if not self._pool:
            return False
        normalized_document = str(document_id or "").strip()
        normalized_execution = str(execution_id or "").strip()
        if not normalized_document or not normalized_execution:
            return False

        async def _link(conn: Any) -> bool:
            row = await conn.fetchrow(
                f"""
                UPDATE documents
                SET metadata = jsonb_set(
                        COALESCE(metadata, '{{}}'::jsonb),
                        '{{{DOCUMENT_PIPELINE_EXECUTION_KEY}}}',
                        to_jsonb($2::text)
                    )
                WHERE document_id = $1
                RETURNING document_id
                """,
                normalized_document,
                normalized_execution,
            )
            return row is not None

        if connection is not None:
            return await _link(connection)
        async with self._pool.acquire() as conn:
            return await _link(conn)

    async def get_pipeline_execution(
        self,
        execution_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        """Return one execution row (snapshot + manifest + status)."""

        if not self._pool:
            return None
        normalized = str(execution_id or "").strip()
        if not normalized:
            return None

        async def _fetch(conn: Any) -> Any:
            return await conn.fetchrow(
                """
                SELECT execution_id, document_id, dataset_id, action,
                       trigger_source, triggered_by, process_rule_id,
                       input_snapshot, manifest, status, error,
                       created_at, completed_at
                FROM document_pipeline_executions
                WHERE execution_id = $1
                """,
                normalized,
            )

        if connection is not None:
            row = await _fetch(connection)
        else:
            async with self._pool.acquire() as conn:
                row = await _fetch(conn)
        return self._row_to_dict(row) if row is not None else None

    async def get_latest_pipeline_execution(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest execution row for one document, if any."""

        if not self._pool:
            return None
        normalized = str(document_id or "").strip()
        if not normalized:
            return None

        async def _fetch(conn: Any) -> Any:
            return await conn.fetchrow(
                """
                SELECT execution_id, document_id, dataset_id, action,
                       trigger_source, triggered_by, process_rule_id,
                       input_snapshot, manifest, status, error,
                       created_at, completed_at
                FROM document_pipeline_executions
                WHERE document_id = $1
                ORDER BY created_at DESC, execution_id DESC
                LIMIT 1
                """,
                normalized,
            )

        if connection is not None:
            row = await _fetch(connection)
        else:
            async with self._pool.acquire() as conn:
                row = await _fetch(conn)
        return self._row_to_dict(row) if row is not None else None

    async def complete_pipeline_execution(
        self,
        execution_id: str,
        *,
        status: str,
        error: str | None = None,
        manifest: dict[str, Any] | list[Any] | None = None,
        connection: Any | None = None,
    ) -> bool:
        """Close one execution row; the manifest records the revision flip."""

        if not self._pool:
            return False
        normalized = str(execution_id or "").strip()
        if not normalized:
            return False
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"completed", "error"}:
            raise ValueError("pipeline execution status must be completed or error")

        async def _close(conn: Any) -> bool:
            if manifest is None:
                row = await conn.fetchrow(
                    """
                    UPDATE document_pipeline_executions
                    SET status = $2,
                        error = $3,
                        completed_at = NOW()
                    WHERE execution_id = $1 AND status = 'running'
                    RETURNING execution_id
                    """,
                    normalized,
                    normalized_status,
                    str(error or "").strip() or None,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE document_pipeline_executions
                    SET status = $2,
                        error = $3,
                        manifest = $4::jsonb,
                        completed_at = NOW()
                    WHERE execution_id = $1 AND status = 'running'
                    RETURNING execution_id
                    """,
                    normalized,
                    normalized_status,
                    str(error or "").strip() or None,
                    json.dumps(manifest),
                )
            return row is not None

        if connection is not None:
            return await _close(connection)
        async with self._pool.acquire() as conn:
            return await _close(conn)

    # ------------------------------------------------------------------
    # Process-rule snapshots (PRD T1 item 7). A rule row is an immutable,
    # content-addressed snapshot of the complete index configuration that
    # actually built a generation: {"index_config", "chunking",
    # "processing_mode"}. Rows
    # are pinned onto documents at generation-open and referenced from
    # document_pipeline_executions.process_rule_id; replay verbs (reprocess/
    # recover) require both snapshots to exist and agree before processing.
    # ------------------------------------------------------------------

    async def record_process_rule(
        self,
        dataset_id: str,
        *,
        mode: str,
        rules: dict[str, Any],
        created_by: str | None = None,
        connection: Any | None = None,
    ) -> str | None:
        """Return the rule id for this (dataset, mode, rules) content.

        Content-dedup by jsonb equality keeps the rule id stable while the
        dataset config is unchanged. The dedup is best-effort (select-then-
        insert): a concurrent race may leave one extra immutable row for the
        same content, which is harmless — every row is frozen by the
        migration-103 immutability trigger and pins resolve per row.
        """

        if not self._pool:
            return None
        normalized_dataset = str(dataset_id or "").strip()
        normalized_mode = str(mode or "").strip().lower()
        if not normalized_dataset or not normalized_mode:
            return None
        if not isinstance(rules, dict):
            raise ValueError("process rule snapshot must be a dict")
        payload = json.dumps(rules)
        rule_id = uuid.uuid4().hex

        async def _record(conn: Any) -> str:
            existing = await conn.fetchval(
                """
                SELECT id
                FROM dataset_process_rules
                WHERE dataset_id = $1 AND mode = $2 AND rules = $3::jsonb
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                normalized_dataset,
                normalized_mode,
                payload,
            )
            if existing is not None:
                return str(existing)
            row = await conn.fetchrow(
                """
                INSERT INTO dataset_process_rules (
                    id, dataset_id, mode, rules, created_by
                )
                VALUES ($1, $2, $3, $4::jsonb, $5)
                RETURNING id
                """,
                rule_id,
                normalized_dataset,
                normalized_mode,
                payload,
                str(created_by or "").strip() or None,
            )
            return str(row["id"]) if row is not None else rule_id

        if connection is not None:
            return await _record(connection)
        async with self._pool.acquire() as conn:
            return await _record(conn)

    async def get_process_rule(
        self,
        process_rule_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any] | None:
        """Return one immutable rule snapshot row, if it exists."""

        if not self._pool:
            return None
        normalized = str(process_rule_id or "").strip()
        if not normalized:
            return None

        async def _fetch(conn: Any) -> Any:
            return await conn.fetchrow(
                """
                SELECT id, dataset_id, mode, rules, created_by, created_at
                FROM dataset_process_rules
                WHERE id = $1
                """,
                normalized,
            )

        if connection is not None:
            row = await _fetch(connection)
        else:
            async with self._pool.acquire() as conn:
                row = await _fetch(conn)
        return self._row_to_dict(row) if row is not None else None

    async def pin_document_process_rule(
        self,
        document_id: str,
        process_rule_id: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """Pin the rule snapshot that governs this document's generations.

        The pin is cross-checked against the execution row during replay. A
        missing or disagreeing row is terminal; replay never uses live config.
        """

        if not self._pool:
            return False
        normalized_document = str(document_id or "").strip()
        normalized_rule = str(process_rule_id or "").strip()
        if not normalized_document or not normalized_rule:
            return False

        async def _pin(conn: Any) -> bool:
            row = await conn.fetchrow(
                """
                UPDATE documents
                SET process_rule_id = $2
                WHERE document_id = $1
                RETURNING document_id
                """,
                normalized_document,
                normalized_rule,
            )
            return row is not None

        if connection is not None:
            return await _pin(connection)
        async with self._pool.acquire() as conn:
            return await _pin(conn)

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

    async def bump_dataset_content_revision(
        self,
        dataset_id: str,
        *,
        connection: Any | None = None,
    ) -> bool:
        """Advance the dataset's authoritative content revision.

        Retrieval cache keys and lexical-transition identities are bound to
        ``content_revision`` via the dataset revision fingerprint (PRD T1
        unified lifecycle contract, §885-886; retrieval cache contract §129:
        写后不可能读到旧值). Every transition that changes which content is
        visible for retrieval must advance the revision so a cached result
        can never outlive the transition. The restore direction bumps
        atomically inside the activation status write instead.

        Deployments also carry the 076 provenance triggers, which advance
        the revision on retrieval-effective documents/segments writes; this
        writer is the knowledge service's own explicit guarantee, kept
        independent of that platform trigger so the cache contract holds
        even on a schema provisioned without it. Extra advancement is
        harmless — nothing depends on revision adjacency.
        """

        normalized_dataset = str(dataset_id or "").strip()
        if not normalized_dataset:
            raise ValueError("dataset_id is required")
        query = """
            UPDATE datasets
            SET content_revision = COALESCE(content_revision, 0) + 1,
                updated_at = NOW()
            WHERE dataset_id = $1
              AND is_deleted = FALSE
            RETURNING dataset_id
        """
        if connection is not None:
            row = await connection.fetchrow(query, normalized_dataset)
            return row is not None
        if not self._pool:
            raise RuntimeError("database is not connected")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, normalized_dataset)
            return row is not None

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

    async def record_dataset_query(
        self,
        *,
        dataset_id: str,
        content: str,
        source: str = "api",
        source_app_id: str | None = None,
        created_by_role: str | None = None,
        created_by: str | None = None,
        metadata: dict[str, Any] | None = None,
        trace_id: str | None = None,
        query_fingerprint: str | None = None,
        mode: str | None = None,
        top_k: int | None = None,
        hit_count: int | None = None,
        stage_timings: dict[str, Any] | None = None,
        segment_ids: list[str] | None = None,
    ) -> bool:
        """Append one retrieval-query telemetry row.

        Telemetry contract (PRD C1): independent transaction, never raises.
        A failure here must not surface to the retrieve() caller, so errors
        are logged and swallowed.
        """
        if not self._pool:
            return False
        try:
            async with self._pool.acquire() as conn:
                if trace_id:
                    async with conn.transaction():
                        inserted = await conn.fetchval(
                            """
                            INSERT INTO dataset_queries (
                                dataset_id, content, source, source_app_id,
                                created_by_role, created_by, metadata,
                                trace_id, query_fingerprint, mode, top_k,
                                hit_count, stage_timings
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7::jsonb,
                                $8::uuid, $9, $10, $11, $12, $13::jsonb
                            )
                            ON CONFLICT (trace_id) WHERE trace_id IS NOT NULL
                            DO NOTHING
                            RETURNING 1
                            """,
                            dataset_id,
                            content,
                            source,
                            source_app_id,
                            created_by_role,
                            created_by,
                            json.dumps(metadata or {}, ensure_ascii=False),
                            trace_id,
                            query_fingerprint,
                            mode,
                            top_k,
                            hit_count,
                            json.dumps(stage_timings or {}, ensure_ascii=False),
                        )
                        if inserted and segment_ids:
                            await conn.execute(
                                """
                                UPDATE segments
                                SET hit_count = hit_count + 1
                                WHERE dataset_id = $1
                                  AND segment_id = ANY($2::text[])
                                """,
                                dataset_id,
                                sorted(set(segment_ids)),
                            )
                    return True

                await conn.execute(
                    """
                    INSERT INTO dataset_queries (
                        dataset_id, content, source, source_app_id,
                        created_by_role, created_by, metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    dataset_id,
                    content,
                    source,
                    source_app_id,
                    created_by_role,
                    created_by,
                    json.dumps(metadata or {}, ensure_ascii=False),
                )
            return True
        except Exception as exc:  # telemetry must never break retrieval
            logger.warning("dataset query telemetry insert failed: %s", exc)
            return False

    async def list_dataset_queries(
        self,
        *,
        dataset_id: str,
        tenant_id: str,
        limit: int,
        zero_results: bool | None = None,
        mode: str | None = None,
        cursor_created_at: Any | None = None,
        cursor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one tenant-bound keyset page of query observations."""

        if not self._pool:
            return []
        clauses = ["q.dataset_id = $1", "d.tenant_id = $2", "d.is_deleted = FALSE"]
        params: list[Any] = [dataset_id, tenant_id]
        if zero_results is not None:
            params.append(0)
            operator = "=" if zero_results else ">"
            clauses.append(f"q.hit_count {operator} ${len(params)}")
        if mode:
            params.append(mode)
            clauses.append(f"q.mode = ${len(params)}")
        if cursor_created_at is not None and cursor_id:
            params.extend([cursor_created_at, cursor_id])
            clauses.append(f"(q.created_at, q.id) < (${len(params) - 1}, ${len(params)})")
        params.append(limit)
        query = f"""
            SELECT q.id, q.dataset_id, q.content, q.source, q.source_app_id,
                   q.created_by_role, q.created_by, q.metadata, q.trace_id,
                   q.query_fingerprint, q.mode, q.top_k, q.hit_count,
                   q.stage_timings, q.created_at
            FROM dataset_queries AS q
            JOIN datasets AS d ON d.dataset_id = q.dataset_id
            WHERE {' AND '.join(clauses)}
            ORDER BY q.created_at DESC, q.id DESC
            LIMIT ${len(params)}
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def get_dataset_query_fingerprint(
        self,
        *,
        dataset_id: str,
        tenant_id: str,
        trace_id: str,
    ) -> str | None:
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT q.query_fingerprint
                FROM dataset_queries AS q
                JOIN datasets AS d ON d.dataset_id = q.dataset_id
                WHERE q.dataset_id = $1
                  AND d.tenant_id = $2
                  AND d.is_deleted = FALSE
                  AND q.trace_id = $3::uuid
                """,
                dataset_id,
                tenant_id,
                trace_id,
            )

    async def upsert_dataset_query_feedback(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        trace_id: str,
        query_fingerprint: str,
        target_type: str,
        target_id: str,
        rating: str,
        reason_code: str,
        comment: str | None,
        created_by: str,
    ) -> dict[str, Any] | None:
        if not self._pool:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO knowledge.dataset_query_feedback (
                    tenant_id, dataset_id, trace_id, query_fingerprint,
                    target_type, target_id, rating, reason_code, comment, created_by
                ) VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (
                    tenant_id, dataset_id, trace_id, target_type, target_id, created_by
                ) DO UPDATE SET
                    query_fingerprint = EXCLUDED.query_fingerprint,
                    rating = EXCLUDED.rating,
                    reason_code = EXCLUDED.reason_code,
                    comment = EXCLUDED.comment,
                    updated_at = NOW()
                RETURNING *
                """,
                tenant_id,
                dataset_id,
                trace_id,
                query_fingerprint,
                target_type,
                target_id,
                rating,
                reason_code,
                comment,
                created_by,
            )
        return self._row_to_dict(row) if row else None

    async def list_dataset_query_feedback(
        self,
        *,
        tenant_id: str,
        dataset_id: str,
        limit: int,
        rating: str | None = None,
        reason_code: str | None = None,
        target_type: str | None = None,
        trace_id: str | None = None,
        cursor_created_at: Any | None = None,
        cursor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self._pool:
            return []
        clauses = [
            "f.tenant_id = $1",
            "f.dataset_id = $2",
            "d.tenant_id = $1",
            "d.is_deleted = FALSE",
        ]
        params: list[Any] = [tenant_id, dataset_id]
        for column, value in (
            ("rating", rating),
            ("reason_code", reason_code),
            ("target_type", target_type),
        ):
            if value:
                params.append(value)
                clauses.append(f"f.{column} = ${len(params)}")
        if trace_id:
            params.append(trace_id)
            clauses.append(f"f.trace_id = ${len(params)}::uuid")
        if cursor_created_at is not None and cursor_id:
            params.extend([cursor_created_at, cursor_id])
            clauses.append(
                f"(f.created_at, f.feedback_id) < "
                f"(${len(params) - 1}, ${len(params)}::uuid)"
            )
        params.append(limit)
        query = f"""
            SELECT f.*, q.content AS query_content
            FROM knowledge.dataset_query_feedback AS f
            JOIN datasets AS d ON d.dataset_id = f.dataset_id
            LEFT JOIN dataset_queries AS q
              ON q.dataset_id = f.dataset_id AND q.trace_id = f.trace_id
            WHERE {' AND '.join(clauses)}
            ORDER BY f.created_at DESC, f.feedback_id DESC
            LIMIT ${len(params)}
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def get_datasets_statistics_batch(
        self, dataset_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """获取多个 Dataset 的统计数据（批量查询优化）"""
        if not self._pool or not dataset_ids:
            return {}

        async with self._pool.acquire() as conn:
            query = """
                WITH document_stats AS (
                    SELECT dataset_id,
                           COUNT(*)::bigint AS document_count,
                           COUNT(*) FILTER (
                               WHERE status = 'completed'
                                 AND enabled = TRUE
                                 AND archived = FALSE
                           )::bigint AS available_document_count,
                           COALESCE(SUM(word_count), 0)::bigint AS word_count
                    FROM documents
                    WHERE dataset_id = ANY($1)
                    GROUP BY dataset_id
                ), segment_stats AS (
                    SELECT dataset_id,
                           COUNT(*)::bigint AS segment_count,
                           COUNT(*) FILTER (
                               WHERE enabled = TRUE AND status = 'completed'
                           )::bigint AS available_segment_count,
                           COALESCE(SUM(hit_count), 0)::bigint AS hit_count
                    FROM segments
                    WHERE dataset_id = ANY($1)
                    GROUP BY dataset_id
                )
                SELECT
                    d.dataset_id,
                    COALESCE(doc.document_count, 0) AS document_count,
                    COALESCE(doc.available_document_count, 0) AS available_document_count,
                    COALESCE(doc.word_count, 0) AS word_count,
                    COALESCE(seg.segment_count, 0) AS segment_count,
                    COALESCE(seg.available_segment_count, 0) AS available_segment_count,
                    COALESCE(seg.hit_count, 0) AS hit_count
                FROM datasets d
                LEFT JOIN document_stats doc ON doc.dataset_id = d.dataset_id
                LEFT JOIN segment_stats seg ON seg.dataset_id = d.dataset_id
                WHERE d.dataset_id = ANY($1)
                  AND d.is_deleted = FALSE
            """
            rows = await conn.fetch(query, dataset_ids)

            result: dict[str, dict[str, int]] = {}
            for row in rows:
                result[row["dataset_id"]] = {
                    "document_count": row["document_count"] or 0,
                    "segment_count": row["segment_count"] or 0,
                    "available_document_count": row["available_document_count"] or 0,
                    "available_segment_count": row["available_segment_count"] or 0,
                    "word_count": row["word_count"] or 0,
                    "hit_count": row["hit_count"] or 0,
                }

            # Ensure all requested dataset_ids have entries (even if empty)
            for ds_id in dataset_ids:
                if ds_id not in result:
                    result[ds_id] = {
                        "document_count": 0,
                        "segment_count": 0,
                        "available_document_count": 0,
                        "available_segment_count": 0,
                        "word_count": 0,
                        "hit_count": 0,
                    }

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
