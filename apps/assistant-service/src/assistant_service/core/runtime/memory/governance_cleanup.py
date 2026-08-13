"""Fail-closed Agent governance cleanup for runtime memory stores."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from ai_gateway_core.agents import (
    RUNTIME_CLEANUP_INVENTORY_SCHEMA,
    RUNTIME_CLEANUP_RECEIPT_SCHEMA,
    canonical_cleanup_digest,
    is_memory_source_handle,
    validate_runtime_cleanup_inventory,
    validate_runtime_cleanup_plan,
)
from ai_gateway_core.logging import record_internal_exception

from ..compat.runtime_adapter import AssistantRuntimeAdapter, AssistantRuntimeFeatures
from .scope import public_source_label, scoped_collection_names

_MAX_PRINCIPALS = 10_000
_MAX_SOURCES = 20_000
_SAFE_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class RuntimeMemoryCleanupError(RuntimeError):
    """Stable error that never embeds storage paths, URLs, or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RuntimeMemoryQdrantCleanupClient:
    """Minimal Qdrant REST surface required by ``MemoryIndexer`` deletion."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._timeout = httpx.Timeout(
            connect=min(5.0, timeout_seconds),
            read=timeout_seconds,
            write=timeout_seconds,
            pool=min(5.0, timeout_seconds),
        )

    @classmethod
    def from_env(cls) -> RuntimeMemoryQdrantCleanupClient | None:
        base_url = os.getenv("ASSISTANT_RUNTIME_QDRANT_URL", "").strip()
        if not base_url:
            return None
        api_key = os.getenv("ASSISTANT_RUNTIME_QDRANT_API_KEY", "").strip() or None
        try:
            timeout = float(os.getenv("ASSISTANT_RUNTIME_QDRANT_TIMEOUT_SECONDS", "10"))
        except ValueError:
            timeout = 10.0
        return cls(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=max(1.0, min(timeout, 60.0)),
        )

    @classmethod
    def from_startup_config(cls, startup_config) -> RuntimeMemoryQdrantCleanupClient | None:
        base_url = str(startup_config.runtime_value("ASSISTANT_RUNTIME_QDRANT_URL"))
        if not base_url:
            return None
        return cls(
            base_url=base_url,
            api_key=startup_config.secret_value("ASSISTANT_RUNTIME_QDRANT_API_KEY") or None,
            timeout_seconds=float(
                startup_config.runtime_value("ASSISTANT_RUNTIME_QDRANT_TIMEOUT_SECONDS")
            ),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    async def delete_points(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
        tenant_id: str,
        user_id: str,
    ) -> None:
        ids = [str(item) for item in point_ids if item]
        if not ids:
            return
        payload = {
            "filter": {
                "must": [
                    {"key": "tenant_id", "match": {"value": tenant_id}},
                    {"key": "user_id", "match": {"value": user_id}},
                    {"has_id": ids},
                ]
            }
        }
        path = f"/collections/{quote(collection_name, safe='')}/points/delete"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                response = await client.post(
                    path,
                    params={"wait": "true"},
                    headers=self._headers(),
                    json=payload,
                )
            if response.status_code == 404:
                return
            response.raise_for_status()
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError("memory_vector_delete_failed") from exc

    async def retrieve_vectors(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
    ) -> dict[str, list[float]]:
        ids = [str(item) for item in point_ids if item]
        if not ids:
            return {}
        path = f"/collections/{quote(collection_name, safe='')}/points"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                response = await client.post(
                    path,
                    headers=self._headers(),
                    json={
                        "ids": ids,
                        "with_payload": False,
                        "with_vector": False,
                    },
                )
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            body = response.json()
        except RuntimeMemoryCleanupError:
            raise
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError("memory_vector_readback_failed") from exc
        records = body.get("result") if isinstance(body, Mapping) else None
        if isinstance(records, Mapping):
            records = records.get("points")
        return {
            str(item.get("id")): []
            for item in (records or [])
            if isinstance(item, Mapping) and item.get("id") is not None
        }

    async def list_points(
        self,
        *,
        collection_name: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Enumerate scoped point ids/payloads for a frozen inventory."""

        path = f"/collections/{quote(collection_name, safe='')}/points/scroll"
        offset: object | None = None
        points: dict[str, dict[str, Any]] = {}
        while True:
            payload: dict[str, Any] = {
                "filter": {
                    "must": [
                        {"key": "tenant_id", "match": {"value": tenant_id}},
                        {"key": "user_id", "match": {"value": user_id}},
                    ]
                },
                "limit": 256,
                "with_payload": True,
                "with_vector": True,
            }
            if offset is not None:
                payload["offset"] = offset
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                ) as client:
                    response = await client.post(
                        path,
                        headers=self._headers(),
                        json=payload,
                    )
                if response.status_code == 404:
                    return {}
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                    exc,
                )
                raise RuntimeMemoryCleanupError("memory_vector_inventory_unavailable") from exc
            result = body.get("result") if isinstance(body, Mapping) else None
            if not isinstance(result, Mapping):
                raise RuntimeMemoryCleanupError("memory_vector_inventory_invalid")
            for item in result.get("points") or []:
                if not isinstance(item, Mapping) or item.get("id") is None:
                    continue
                point_id = str(item["id"])
                point_payload = item.get("payload")
                normalized_payload = (
                    dict(point_payload) if isinstance(point_payload, Mapping) else {}
                )
                if "vector" not in item:
                    raise RuntimeMemoryCleanupError("memory_vector_inventory_invalid")
                try:
                    normalized_payload["_governance_vector_digest"] = canonical_cleanup_digest(
                        {"vector": item.get("vector")}
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeMemoryCleanupError("memory_vector_inventory_invalid") from exc
                points[point_id] = normalized_payload
                if len(points) > _MAX_SOURCES * 20:
                    raise RuntimeMemoryCleanupError("memory_cleanup_scope_too_large")
            next_offset = result.get("next_page_offset")
            if next_offset is None:
                return points
            offset = next_offset

    async def list_collections(self) -> list[str]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                response = await client.get("/collections", headers=self._headers())
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError(
                "memory_vector_collection_inventory_unavailable"
            ) from exc
        result = body.get("result") if isinstance(body, Mapping) else None
        collections = result.get("collections") if isinstance(result, Mapping) else None
        if not isinstance(collections, list):
            raise RuntimeMemoryCleanupError("memory_vector_collection_inventory_invalid")
        return sorted(
            {
                str(item.get("name"))
                for item in collections
                if isinstance(item, Mapping) and str(item.get("name") or "").strip()
            }
        )

    async def list_collection_names(self) -> list[str]:
        """Match the collection-inventory surface used by ``MemoryIndexer``."""

        return await self.list_collections()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, default)


def _as_utc(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: object) -> str:
    parsed = _as_utc(value)
    if parsed is not None:
        return parsed.isoformat()
    return str(value or "").strip()


def _source_type(path: Path) -> str:
    if path.name == "MEMORY.md":
        return "long_term"
    if path.name == "USER.md":
        return "profile"
    if path.name.startswith("REFLECTION-"):
        return "reflection"
    return "daily"


class AgentRuntimeMemoryCleanupService:
    """Freeze and execute point-in-time runtime-memory deletion inventories."""

    def __init__(self, *, database: Any, runtime: AssistantRuntimeAdapter) -> None:
        self.database = database
        self.runtime = runtime

    @classmethod
    def from_env(cls, *, database: Any) -> AgentRuntimeMemoryCleanupService:
        vector_store = RuntimeMemoryQdrantCleanupClient.from_env()
        runtime = AssistantRuntimeAdapter.from_env(
            database=database,
            vector_store=vector_store,
        )
        return cls(database=database, runtime=runtime)

    @classmethod
    def from_startup_config(
        cls,
        *,
        database: Any,
        startup_config,
    ) -> AgentRuntimeMemoryCleanupService:
        vector_store = RuntimeMemoryQdrantCleanupClient.from_startup_config(startup_config)
        runtime = AssistantRuntimeAdapter.from_env(
            database=database,
            vector_store=vector_store,
            base_memory_dir=str(
                startup_config.runtime_value("ASSISTANT_RUNTIME_MEMORY_DIR")
            )
            or None,
            legacy_memory_dir=str(
                startup_config.runtime_value("ASSISTANT_RUNTIME_LEGACY_MEMORY_DIR")
            )
            or None,
            memory_max_source_bytes=int(
                startup_config.runtime_value("ASSISTANT_RUNTIME_MEMORY_MAX_SOURCE_BYTES")
            ),
            features=AssistantRuntimeFeatures(
                memory_v2=startup_config.bool_value("ASSISTANT_RUNTIME_MEMORY_V2"),
                context_v2=startup_config.bool_value("ASSISTANT_RUNTIME_CONTEXT_V2"),
                tool_policy_v2=startup_config.bool_value(
                    "ASSISTANT_RUNTIME_TOOL_POLICY_V2"
                ),
                skills=startup_config.bool_value("ASSISTANT_RUNTIME_SKILLS"),
                scheduler=startup_config.bool_value("ASSISTANT_RUNTIME_SCHEDULER"),
                failover_v2=startup_config.bool_value("ASSISTANT_RUNTIME_FAILOVER_V2"),
            ),
        )
        return cls(database=database, runtime=runtime)

    async def inspect(self, plan_value: object) -> dict[str, Any]:
        plan = validate_runtime_cleanup_plan(plan_value)
        principals = list(plan["principal_handles"])
        if len(principals) > _MAX_PRINCIPALS:
            raise RuntimeMemoryCleanupError("memory_cleanup_scope_too_large")
        cutoff = _as_utc(plan["cutoff_at"])
        if cutoff is None:
            raise RuntimeMemoryCleanupError("memory_cleanup_cutoff_invalid")
        inventory_principals: list[dict[str, Any]] = []
        total_sources = 0
        total_vectors = 0
        for principal_id in principals:
            all_sources = await self._source_manifests(
                tenant_id=plan["tenant_id"],
                principal_id=principal_id,
                cutoff=None,
            )
            sources = await self._source_manifests(
                tenant_id=plan["tenant_id"],
                principal_id=principal_id,
                cutoff=cutoff,
            )
            public_sources = [
                {
                    "logical_source_scope_handle": item["logical_source_scope_handle"],
                    "source_handle": item["source_handle"],
                    "version_digest": item["version_digest"],
                    "source_type": item["source_type"],
                }
                for item in sources.values()
            ]
            public_sources.sort(key=lambda item: item["logical_source_scope_handle"])
            vector_sets = await self._vector_manifests(
                tenant_id=plan["tenant_id"],
                principal_id=principal_id,
                cutoff=cutoff,
                eligible_sources=sources,
                all_sources=all_sources,
            )
            public_vector_sets = [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in vector_sets
            ]
            vector_count = sum(item["point_count"] for item in public_vector_sets)
            total_sources += len(public_sources)
            total_vectors += vector_count
            if total_sources > _MAX_SOURCES:
                raise RuntimeMemoryCleanupError("memory_cleanup_scope_too_large")
            if total_vectors > _MAX_SOURCES * 20:
                raise RuntimeMemoryCleanupError("memory_cleanup_scope_too_large")
            inventory_principals.append(
                {
                    "principal_id": principal_id,
                    "source_count": len(public_sources),
                    "sources": public_sources,
                    "vector_count": vector_count,
                    "vector_sets": public_vector_sets,
                }
            )
        inventory: dict[str, Any] = {
            "schema_version": RUNTIME_CLEANUP_INVENTORY_SCHEMA,
            "deletion_id": plan["deletion_id"],
            "tenant_id": plan["tenant_id"],
            "agent_id": plan["agent_id"],
            "plan_digest": plan["plan_digest"],
            "cutoff_at": plan["cutoff_at"],
            "principal_count": len(inventory_principals),
            "source_count": total_sources,
            "vector_count": total_vectors,
            "principals": inventory_principals,
        }
        inventory["inventory_digest"] = canonical_cleanup_digest(inventory)
        return inventory

    async def execute(
        self,
        *,
        plan_value: object,
        inventory_value: object,
    ) -> dict[str, Any]:
        plan = validate_runtime_cleanup_plan(plan_value)
        inventory = validate_runtime_cleanup_inventory(
            inventory_value,
            plan=plan,
        )
        principal_receipts: list[dict[str, Any]] = []
        for frozen_principal in inventory["principals"]:
            principal_receipts.append(
                await self._execute_principal(
                    tenant_id=plan["tenant_id"],
                    principal_id=frozen_principal["principal_id"],
                    frozen_sources=frozen_principal["sources"],
                    frozen_vector_sets=frozen_principal["vector_sets"],
                )
            )
        completed = all(item["completed"] for item in principal_receipts)
        receipt: dict[str, Any] = {
            "schema_version": RUNTIME_CLEANUP_RECEIPT_SCHEMA,
            "deletion_id": plan["deletion_id"],
            "tenant_id": plan["tenant_id"],
            "agent_id": plan["agent_id"],
            "plan_digest": plan["plan_digest"],
            "inventory_digest": inventory["inventory_digest"],
            "status": "completed" if completed else "partial",
            "completed": completed,
            "retryable": not completed,
            "principals": principal_receipts,
            "errors": sorted(
                {str(error) for item in principal_receipts for error in item.get("errors", [])}
            ),
        }
        receipt["receipt_digest"] = canonical_cleanup_digest(receipt)
        return receipt

    async def _execute_principal(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        frozen_sources: list[dict[str, Any]],
        frozen_vector_sets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        frozen_vector_count = sum(len(vector_set["points"]) for vector_set in frozen_vector_sets)
        current = await self._source_manifests(
            tenant_id=tenant_id,
            principal_id=principal_id,
            cutoff=None,
        )
        frozen_by_scope = {
            str(item["logical_source_scope_handle"]): item for item in frozen_sources
        }
        changed: list[str] = []
        for scope_handle, frozen in frozen_by_scope.items():
            manifest = current.get(scope_handle)
            if manifest is None:
                continue
            if manifest["source_handle"] != frozen["source_handle"]:
                changed.append(scope_handle)
                continue
            if manifest["version_digest"] == frozen["version_digest"]:
                continue
            database_generation = manifest.get("_database_generation")
            expected_deletion_progress = (
                manifest.get("_deletion_stage") == "finalizing" and database_generation is None
            ) or (
                manifest.get("_source_status") == "deletion_pending"
                and isinstance(database_generation, Mapping)
                and database_generation.get("kind") == "scoped_sql_memory_tombstone/v1"
                and database_generation.get("deletion_pending") is True
                and database_generation.get("persisted_source_handle") == frozen["source_handle"]
            )
            if not expected_deletion_progress:
                changed.append(scope_handle)
        if changed:
            return {
                "principal_id": principal_id,
                "status": "partial",
                "completed": False,
                "retryable": True,
                "source_count": len(frozen_sources),
                "deleted_source_count": 0,
                "vector_count": frozen_vector_count,
                "deleted_vector_count": 0,
                "idempotent_absent_count": sum(
                    1 for scope_handle in frozen_by_scope if scope_handle not in current
                ),
                "idempotent_absent_vector_count": 0,
                "errors": ["memory_source_changed_since_prepare"],
            }

        try:
            current_vector_sets = await self._vector_manifests(
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
        except RuntimeMemoryCleanupError as exc:
            return {
                "principal_id": principal_id,
                "status": "partial",
                "completed": False,
                "retryable": True,
                "source_count": len(frozen_sources),
                "deleted_source_count": 0,
                "vector_count": frozen_vector_count,
                "deleted_vector_count": 0,
                "idempotent_absent_count": sum(
                    1 for scope_handle in frozen_by_scope if scope_handle not in current
                ),
                "idempotent_absent_vector_count": 0,
                "errors": [exc.code],
            }

        frozen_vectors = {
            (str(vector_set["collection_handle"]), str(point["point_id"])): point
            for vector_set in frozen_vector_sets
            for point in vector_set["points"]
        }
        current_vectors = {
            (str(vector_set["collection_handle"]), str(point["point_id"])): point
            for vector_set in current_vector_sets
            for point in vector_set["points"]
        }
        absent_vectors = sum(1 for key in frozen_vectors if key not in current_vectors)
        changed_vectors = [
            key
            for key, frozen in frozen_vectors.items()
            if key in current_vectors
            and current_vectors[key]["version_digest"] != frozen["version_digest"]
        ]
        if changed_vectors:
            return {
                "principal_id": principal_id,
                "status": "partial",
                "completed": False,
                "retryable": True,
                "source_count": len(frozen_sources),
                "deleted_source_count": 0,
                "vector_count": len(frozen_vectors),
                "deleted_vector_count": 0,
                "idempotent_absent_count": sum(
                    1 for scope_handle in frozen_by_scope if scope_handle not in current
                ),
                "idempotent_absent_vector_count": absent_vectors,
                "errors": ["memory_vector_changed_since_prepare"],
            }

        deleted = 0
        deleted_vectors = 0
        absent = 0
        errors: list[str] = []
        for scope_handle in sorted(frozen_by_scope):
            frozen_source = frozen_by_scope[scope_handle]
            manifest = current.get(scope_handle)
            if manifest is None:
                absent += 1
                continue
            result = await self.runtime.delete_memory_source_by_id(
                tenant_id=tenant_id,
                user_id=principal_id,
                source_id=frozen_source["source_handle"],
                expected_database_source_handle=(
                    str(manifest.get("_database_source_handle") or "") or None
                ),
            )
            read_back = getattr(result, "read_back", {})
            deletion_proven = (
                result.completed is True
                and str(getattr(result, "source_id", "")) == frozen_source["source_handle"]
                and isinstance(read_back, Mapping)
                and read_back.get("file_absent") is True
                and read_back.get("sql_source_absent") is True
                and read_back.get("sql_chunks_absent") is True
                and read_back.get("vector_points_remaining") == 0
            )
            if deletion_proven:
                deleted += 1
            else:
                stable_errors = [
                    str(item)
                    for item in getattr(result, "errors", ())
                    if _SAFE_SOURCE_TYPE_RE.fullmatch(str(item))
                ]
                errors.extend(stable_errors or ["memory_source_delete_unverified"])
        vector_store = getattr(self.runtime.memory_indexer, "vector_store", None)
        if errors:
            # A source-generation fence failed after the public inventory was
            # revalidated.  Do not apply the older vector snapshot: a concurrent
            # reindex may have reused a point id for a new generation.
            pass
        elif vector_store is None:
            errors.append("memory_vector_cleanup_unavailable")
        else:
            collections_by_handle = {
                str(item["collection_handle"]): str(item["_collection_name"])
                for item in current_vector_sets
            }
            for vector_set in frozen_vector_sets:
                collection_handle = str(vector_set["collection_handle"])
                point_ids = [
                    str(item["point_id"])
                    for item in vector_set["points"]
                    if (collection_handle, str(item["point_id"])) in current_vectors
                ]
                collection_name = collections_by_handle.get(collection_handle)
                if not point_ids or collection_name is None:
                    continue
                try:
                    await vector_store.delete_points(
                        collection_name=collection_name,
                        point_ids=point_ids,
                        tenant_id=tenant_id,
                        user_id=principal_id,
                    )
                    remaining_vectors = await vector_store.retrieve_vectors(
                        collection_name=collection_name,
                        point_ids=point_ids,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                        exc,
                    )
                    errors.append("memory_vector_delete_failed")
                    continue
                if remaining_vectors:
                    errors.append("memory_vector_delete_readback_failed")
                else:
                    deleted_vectors += len(point_ids)
        remaining = await self._source_manifests(
            tenant_id=tenant_id,
            principal_id=principal_id,
            cutoff=None,
        )
        remaining_frozen = sorted(set(frozen_by_scope).intersection(remaining))
        if remaining_frozen:
            errors.append("memory_source_delete_readback_failed")
        try:
            remaining_vector_sets = await self._vector_manifests(
                tenant_id=tenant_id,
                principal_id=principal_id,
            )
            remaining_vectors = {
                (str(vector_set["collection_handle"]), str(point["point_id"]))
                for vector_set in remaining_vector_sets
                for point in vector_set["points"]
            }
            if set(frozen_vectors).intersection(remaining_vectors):
                errors.append("memory_vector_delete_readback_failed")
        except RuntimeMemoryCleanupError as exc:
            errors.append(exc.code)
        completed = not remaining_frozen and not errors
        return {
            "principal_id": principal_id,
            "status": "completed" if completed else "partial",
            "completed": completed,
            "retryable": not completed,
            "source_count": len(frozen_sources),
            "deleted_source_count": deleted,
            "vector_count": len(frozen_vectors),
            "deleted_vector_count": deleted_vectors,
            "idempotent_absent_count": absent,
            "idempotent_absent_vector_count": absent_vectors,
            "errors": list(dict.fromkeys(errors)),
        }

    async def _source_manifests(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        cutoff: datetime | None,
    ) -> dict[str, dict[str, Any]]:
        authorities = await self._source_authorities(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )
        try:
            rows = await self.database.fetch(
                """
                SELECT s.source_id, s.source_path, s.source_type,
                       s.content_hash, s.created_at, s.updated_at, s.metadata,
                       c.chunk_id
                FROM assistant_memory_sources s
                LEFT JOIN assistant_memory_chunks c
                  ON c.source_id = s.source_id
                 AND c.tenant_id = $1 AND c.user_id = $2
                WHERE s.tenant_id = $1 AND s.user_id = $2
                  AND LOWER(COALESCE(
                      s.metadata->>'deletion_completed',
                      'false'
                  )) <> 'true'
                ORDER BY s.source_id, c.chunk_id
                """,
                tenant_id,
                principal_id,
            )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError("memory_cleanup_manifest_unavailable") from exc

        database_sources: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            metadata = _row_value(row, "metadata", {}) or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    raise RuntimeMemoryCleanupError("memory_cleanup_manifest_invalid")
            if not isinstance(metadata, Mapping):
                raise RuntimeMemoryCleanupError("memory_cleanup_manifest_invalid")
            persisted_collections = (
                [
                    str(item)
                    for item in metadata.get("vector_collections", [])
                    if str(item or "").strip()
                ]
                if isinstance(metadata.get("vector_collections"), list)
                else []
            )
            persisted_source_handle = str(metadata.get("deletion_source_handle") or "").strip()
            if persisted_source_handle and not is_memory_source_handle(persisted_source_handle):
                raise RuntimeMemoryCleanupError("memory_cleanup_source_handle_invalid")
            source_id = str(_row_value(row, "source_id") or "").strip()
            source_path = str(_row_value(row, "source_path") or "").strip()
            if not source_id or not source_path:
                raise RuntimeMemoryCleanupError("memory_cleanup_manifest_invalid")
            entry = database_sources.setdefault(
                source_id,
                {
                    "sql_source_id": source_id,
                    "_db_path": source_path,
                    "_label": public_source_label(source_path),
                    "source_type": str(_row_value(row, "source_type") or "unknown"),
                    "sql_content_hash": str(_row_value(row, "content_hash") or ""),
                    "sql_created_at": _as_utc(_row_value(row, "created_at")),
                    "sql_updated_at": _as_utc(_row_value(row, "updated_at")),
                    "persisted_source_handle": persisted_source_handle,
                    "indexing_token": str(metadata.get("indexing_token") or ""),
                    "deletion_pending": bool(persisted_source_handle)
                    or (str(metadata.get("deletion_pending") or "false").lower() == "true"),
                    "chunk_ids": [],
                    "vector_collections": persisted_collections,
                },
            )
            if (
                entry["_db_path"] != source_path
                or entry["source_type"] != str(_row_value(row, "source_type") or "unknown")
                or entry["persisted_source_handle"] != persisted_source_handle
                or entry["sql_content_hash"] != str(_row_value(row, "content_hash") or "")
                or entry["sql_created_at"] != _as_utc(_row_value(row, "created_at"))
                or entry["sql_updated_at"] != _as_utc(_row_value(row, "updated_at"))
                or entry["indexing_token"] != str(metadata.get("indexing_token") or "")
                or entry["deletion_pending"]
                != (
                    bool(persisted_source_handle)
                    or str(metadata.get("deletion_pending") or "false").lower() == "true"
                )
            ):
                raise RuntimeMemoryCleanupError("memory_cleanup_manifest_invalid")
            entry["vector_collections"] = list(
                dict.fromkeys([*entry.get("vector_collections", []), *persisted_collections])
            )
            chunk_id = str(_row_value(row, "chunk_id") or "")
            if chunk_id:
                entry["chunk_ids"].append(chunk_id)

        scoped_records: list[dict[str, Any]] = []
        list_records = getattr(
            self.runtime.memory_indexer,
            "list_scoped_source_records",
            None,
        )
        if callable(list_records):
            try:
                records_value = await list_records(
                    tenant_id=tenant_id,
                    user_id=principal_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                    exc,
                )
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_unavailable") from exc
            if not isinstance(records_value, list):
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
            scoped_records = [dict(item) for item in records_value if isinstance(item, Mapping)]

        scoped_by_handle: dict[str, dict[str, Any]] = {}
        scoped_by_source_id: dict[str, dict[str, Any]] = {}
        for record in scoped_records:
            source_handle = str(record.get("source_handle") or "")
            source_id = str(record.get("source_id") or "")
            if (
                not is_memory_source_handle(source_handle)
                or not source_id
                or record.get("owner_proven") is not True
            ):
                continue
            if source_handle in scoped_by_handle or source_id in scoped_by_source_id:
                raise RuntimeMemoryCleanupError("memory_cleanup_database_generation_ambiguous")
            scoped_by_handle[source_handle] = record
            scoped_by_source_id[source_id] = record
        claimed_database_sources: set[str] = set()
        manifests: dict[str, dict[str, Any]] = {}
        for scope_handle, authority in authorities.items():
            source_handle = authority["source_handle"]
            local_record = self.runtime.memory_store.resolve_source_handle_record(
                tenant_id,
                principal_id,
                source_handle,
            )
            if local_record is None and scoped_records:
                local_record = self.runtime.memory_store.resolve_legacy_source_handle_record(
                    tenant_id,
                    principal_id,
                    source_handle,
                    scoped_records,
                )

            exact_candidates: set[str] = set()
            sql_record = scoped_by_handle.get(source_handle)
            if sql_record is not None:
                sql_source_id = str(sql_record.get("source_id") or "")
                if sql_source_id in database_sources:
                    exact_candidates.add(sql_source_id)
            for source_id, db_item in database_sources.items():
                if db_item["persisted_source_handle"] == source_handle:
                    exact_candidates.add(source_id)
                if local_record is None:
                    continue
                index_path = str(local_record.get("_index_source_path") or "")
                if index_path and index_path == db_item["_db_path"]:
                    exact_candidates.add(source_id)
                    continue
                current_target = self.runtime.memory_store.resolve_owned_source(
                    tenant_id,
                    principal_id,
                    db_item["_db_path"],
                )
                legacy_target = self.runtime.memory_store.resolve_legacy_owned_source(
                    tenant_id,
                    principal_id,
                    db_item["_db_path"],
                    owner_proven=True,
                )
                if Path(str(local_record["_path"])) in {
                    item for item in (current_target, legacy_target) if item is not None
                }:
                    exact_candidates.add(source_id)

            if len(exact_candidates) > 1:
                raise RuntimeMemoryCleanupError("memory_cleanup_source_scope_ambiguous")
            database_source: dict[str, Any] | None = None
            if exact_candidates:
                source_id = next(iter(exact_candidates))
                database_source = database_sources[source_id]
                claimed_database_sources.add(source_id)
            else:
                label_candidates = [
                    (source_id, item)
                    for source_id, item in database_sources.items()
                    if source_id not in claimed_database_sources
                    and item["_label"] == authority["_label"]
                    and item["source_type"] == authority["source_type"]
                ]
                if len(label_candidates) > 1:
                    raise RuntimeMemoryCleanupError("memory_cleanup_source_scope_ambiguous")
                if label_candidates:
                    source_id, database_source = label_candidates[0]
                    claimed_database_sources.add(source_id)

            database_generation_record = (
                scoped_by_source_id.get(str(database_source.get("sql_source_id") or ""))
                if database_source is not None
                else None
            )
            if (
                database_source is not None
                and database_source.get("deletion_pending") is not True
                and database_generation_record is None
            ):
                raise RuntimeMemoryCleanupError("memory_cleanup_database_generation_unverified")
            if database_source is not None and database_generation_record is not None:
                raw_generation = {
                    "source_id": str(database_source.get("sql_source_id") or ""),
                    "source_path": str(database_source.get("_db_path") or ""),
                    "source_type": str(database_source.get("source_type") or "unknown"),
                    "content_hash": str(database_source.get("sql_content_hash") or ""),
                    "created_at": _canonical_timestamp(database_source.get("sql_created_at")),
                    "updated_at": _canonical_timestamp(database_source.get("sql_updated_at")),
                    "chunk_ids": sorted(
                        str(item) for item in (database_source.get("chunk_ids") or [])
                    ),
                    "vector_collections": sorted(
                        str(item) for item in (database_source.get("vector_collections") or [])
                    ),
                    "indexing_token": str(database_source.get("indexing_token") or ""),
                }
                scoped_generation = {
                    "source_id": str(database_generation_record.get("source_id") or ""),
                    "source_path": str(database_generation_record.get("source_path") or ""),
                    "source_type": str(database_generation_record.get("source_type") or "unknown"),
                    "content_hash": str(database_generation_record.get("content_hash") or ""),
                    "created_at": _canonical_timestamp(
                        database_generation_record.get("created_at")
                    ),
                    "updated_at": _canonical_timestamp(
                        database_generation_record.get("updated_at")
                    ),
                    "chunk_ids": sorted(
                        str(item) for item in (database_generation_record.get("chunk_ids") or [])
                    ),
                    "vector_collections": sorted(
                        str(item)
                        for item in (database_generation_record.get("vector_collections") or [])
                    ),
                    "indexing_token": str(database_generation_record.get("indexing_token") or ""),
                }
                if raw_generation != scoped_generation:
                    raise RuntimeMemoryCleanupError("memory_cleanup_database_generation_changed")

            if authority.get("derived_only") is True and (
                database_source is None or sql_record is None
            ):
                raise RuntimeMemoryCleanupError("memory_cleanup_database_source_unverified")
            local_deletion_stage = (
                str(local_record.get("_deletion_stage") or "") if local_record is not None else ""
            )
            if (
                authority["status"] == "deletion_pending"
                and database_source is None
                and local_deletion_stage not in {"staged", "finalizing"}
            ):
                raise RuntimeMemoryCleanupError("memory_cleanup_pending_source_unverified")

            file_updated_at = self._source_record_updated_at(
                local_record=local_record,
                source_handle=source_handle,
            )
            sql_created_at = database_source.get("sql_created_at") if database_source else None
            sql_updated_at = database_source.get("sql_updated_at") if database_source else None
            source_updated_at = file_updated_at
            if source_updated_at is None:
                source_updated_at = (
                    sql_updated_at
                    if authority.get("derived_only") is True
                    else sql_created_at or sql_updated_at
                )
            if source_updated_at is None:
                raise RuntimeMemoryCleanupError("memory_cleanup_source_timestamp_unknown")
            if cutoff is not None and source_updated_at > cutoff:
                continue

            manifest_entry: dict[str, Any] = {
                "logical_source_scope_handle": scope_handle,
                "source_handle": source_handle,
                "source_type": authority["source_type"],
                "sql_source_id": (database_source.get("sql_source_id") if database_source else ""),
                "sql_content_hash": (
                    database_source.get("sql_content_hash") if database_source else ""
                ),
                "sql_created_at": sql_created_at,
                "chunk_ids": sorted(
                    set(database_source.get("chunk_ids") or []) if database_source else set()
                ),
                "vector_collections": sorted(
                    set(database_source.get("vector_collections") or [])
                    if database_source
                    else set()
                ),
            }
            database_generation: dict[str, Any] | None = None
            database_source_handle = ""
            if database_generation_record is not None:
                database_source_handle = str(database_generation_record.get("source_handle") or "")
                database_generation = {
                    "kind": "scoped_sql_memory_source/v1",
                    "tenant_id": tenant_id,
                    "user_id": principal_id,
                    "source_id": str(database_generation_record.get("source_id") or ""),
                    "source_path": str(database_generation_record.get("source_path") or ""),
                    "source_type": str(database_generation_record.get("source_type") or "unknown"),
                    "content_hash": str(database_generation_record.get("content_hash") or ""),
                    "created_at": _canonical_timestamp(
                        database_generation_record.get("created_at")
                    ),
                    "updated_at": _canonical_timestamp(
                        database_generation_record.get("updated_at")
                    ),
                    "chunk_ids": sorted(
                        str(item) for item in (database_generation_record.get("chunk_ids") or [])
                    ),
                    "vector_collections": sorted(
                        str(item)
                        for item in (database_generation_record.get("vector_collections") or [])
                    ),
                    "indexing_token": str(database_generation_record.get("indexing_token") or ""),
                    "source_handle": database_source_handle,
                }
            elif database_source is not None:
                database_generation = {
                    "kind": "scoped_sql_memory_tombstone/v1",
                    "tenant_id": tenant_id,
                    "user_id": principal_id,
                    "source_id": str(database_source.get("sql_source_id") or ""),
                    "source_path": str(database_source.get("_db_path") or ""),
                    "source_type": str(database_source.get("source_type") or "unknown"),
                    "content_hash": str(database_source.get("sql_content_hash") or ""),
                    "created_at": _canonical_timestamp(database_source.get("sql_created_at")),
                    "updated_at": _canonical_timestamp(database_source.get("sql_updated_at")),
                    "chunk_ids": sorted(
                        str(item) for item in (database_source.get("chunk_ids") or [])
                    ),
                    "vector_collections": sorted(
                        str(item) for item in (database_source.get("vector_collections") or [])
                    ),
                    "indexing_token": str(database_source.get("indexing_token") or ""),
                    "persisted_source_handle": str(
                        database_source.get("persisted_source_handle") or ""
                    ),
                    "deletion_pending": (database_source.get("deletion_pending") is True),
                }
            manifest_entry["_database_source_handle"] = database_source_handle
            manifest_entry["_database_generation"] = database_generation
            manifest_entry["_source_status"] = authority["status"]
            manifest_entry["_deletion_stage"] = local_deletion_stage
            generation_material = {
                "logical_source_scope_handle": scope_handle,
                "source_type": manifest_entry["source_type"],
                "source_handle": source_handle,
                "database_generation": database_generation,
            }
            manifest_entry["version_digest"] = canonical_cleanup_digest(generation_material)
            manifests[scope_handle] = manifest_entry

        if set(database_sources) != claimed_database_sources:
            raise RuntimeMemoryCleanupError("memory_cleanup_database_source_unrepresented")
        return manifests

    async def _source_authorities(
        self,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> dict[str, dict[str, Any]]:
        try:
            inventory = await self.runtime.inspect_memory_sources(
                tenant_id=tenant_id,
                user_id=principal_id,
            )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError("memory_cleanup_inventory_unavailable") from exc
        if not isinstance(inventory, Mapping) or inventory.get("status") != "ok":
            raise RuntimeMemoryCleanupError("memory_cleanup_inventory_unavailable")
        quarantined = inventory.get("legacy_quarantined_sources", 0)
        if not isinstance(quarantined, int) or isinstance(quarantined, bool) or quarantined < 0:
            raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
        if quarantined:
            raise RuntimeMemoryCleanupError("memory_legacy_source_ownership_unproven")
        sources = inventory.get("sources")
        if not isinstance(sources, list):
            raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
        authorities: dict[str, dict[str, Any]] = {}
        observed_handles: set[str] = set()
        for source in sources:
            if not isinstance(source, Mapping):
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
            source_handle = str(source.get("source_id") or "")
            label = str(source.get("label") or "")
            source_type = str(source.get("source_type") or "")
            status = str(source.get("status") or "")
            if (
                source_handle in observed_handles
                or not is_memory_source_handle(source_handle)
                or not label
                or len(label) > 255
                or label in {".", ".."}
                or "/" in label
                or "\\" in label
                or "\x00" in label
                or Path(label).name != label
                or not _SAFE_SOURCE_TYPE_RE.fullmatch(source_type)
                or status not in {"active", "deletion_pending"}
                or source.get("owner_proven") is False
            ):
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
            if "derived_only" in source and source.get("derived_only") is not True:
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_invalid")
            observed_handles.add(source_handle)
            material = f"{tenant_id}\0{principal_id}\0{label}".encode()
            scope_handle = f"memscope_{hashlib.sha256(material).hexdigest()[:32]}"
            if scope_handle in authorities:
                raise RuntimeMemoryCleanupError("memory_cleanup_source_scope_ambiguous")
            authorities[scope_handle] = {
                "source_handle": source_handle,
                "source_type": source_type,
                "status": status,
                "derived_only": source.get("derived_only") is True,
                "_label": label,
            }
        return authorities

    def _source_record_updated_at(
        self,
        *,
        local_record: Mapping[str, Any] | None,
        source_handle: str,
    ) -> datetime | None:
        if local_record is None:
            return None
        candidates: list[Path] = []
        staged_path = str(local_record.get("_staged_path") or "")
        if staged_path:
            candidates.append(Path(staged_path))
        target = Path(str(local_record.get("_path") or ""))
        if str(target):
            candidates.append(target)
            stage = str(local_record.get("_deletion_stage") or "")
            if stage == "staged":
                candidates.append(
                    self.runtime.memory_store._staged_source_path(
                        target,
                        source_handle,
                    )
                )
            elif stage == "finalizing":
                candidates.append(
                    self.runtime.memory_store._finalizing_source_path(
                        target,
                        source_handle,
                    )
                )
        for candidate in dict.fromkeys(candidates):
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    return datetime.fromtimestamp(
                        candidate.stat().st_mtime,
                        tz=timezone.utc,
                    )
            except OSError as exc:
                raise RuntimeMemoryCleanupError("memory_cleanup_inventory_unavailable") from exc
        return None

    async def _vector_manifests(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        cutoff: datetime | None = None,
        eligible_sources: dict[str, dict[str, Any]] | None = None,
        all_sources: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        vector_store = getattr(self.runtime.memory_indexer, "vector_store", None)
        list_points = getattr(vector_store, "list_points", None)
        if not callable(list_points):
            raise RuntimeMemoryCleanupError("memory_vector_inventory_unavailable")

        list_collections = getattr(vector_store, "list_collections", None)
        if not callable(list_collections):
            list_collections = getattr(vector_store, "list_collection_names", None)
        if not callable(list_collections):
            raise RuntimeMemoryCleanupError("memory_vector_collection_inventory_unavailable")
        try:
            available_result = list_collections()
            available = (
                await available_result
                if inspect.isawaitable(available_result)
                else available_result
            )
        except RuntimeMemoryCleanupError:
            raise
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                exc,
            )
            raise RuntimeMemoryCleanupError(
                "memory_vector_collection_inventory_unavailable"
            ) from exc
        if not isinstance(available, (list, tuple, set)):
            raise RuntimeMemoryCleanupError("memory_vector_collection_inventory_invalid")

        base_names = scoped_collection_names(
            self.runtime.memory_indexer.collection_prefix,
            tenant_id,
            principal_id,
        )
        scoped_patterns = [
            (
                "current" if index == 0 else "legacy",
                base_name,
                re.compile(rf"{re.escape(base_name)}_d[1-9][0-9]*"),
            )
            for index, base_name in enumerate(base_names)
        ]

        def collection_kind(collection_name: str) -> str | None:
            for kind, base_name, dimension_pattern in scoped_patterns:
                if collection_name == base_name or dimension_pattern.fullmatch(collection_name):
                    return kind
            return None

        persisted_names = {
            str(collection_name)
            for source in {
                **(all_sources or {}),
                **(eligible_sources or {}),
            }.values()
            for collection_name in source.get("vector_collections", [])
            if str(collection_name or "").strip()
        }
        if any(collection_kind(name) is None for name in persisted_names):
            raise RuntimeMemoryCleanupError("memory_vector_collection_inventory_invalid")
        discovered_names = {
            str(name)
            for name in available
            if str(name or "").strip() and collection_kind(str(name)) is not None
        }
        collection_names = sorted(discovered_names | persisted_names)
        if len(collection_names) > _MAX_SOURCES:
            raise RuntimeMemoryCleanupError("memory_cleanup_scope_too_large")

        eligible_chunk_ids = {
            str(chunk_id)
            for source in (eligible_sources or {}).values()
            for chunk_id in source.get("chunk_ids", [])
        }
        all_chunk_ids = {
            str(chunk_id)
            for source in (all_sources or {}).values()
            for chunk_id in source.get("chunk_ids", [])
        }
        eligible_source_ids = {
            str(source.get("sql_source_id") or "")
            for source in (eligible_sources or {}).values()
            if source.get("sql_source_id")
        }
        all_source_ids = {
            str(source.get("sql_source_id") or "")
            for source in (all_sources or {}).values()
            if source.get("sql_source_id")
        }
        manifests: list[dict[str, Any]] = []
        for collection_name in collection_names:
            kind = collection_kind(collection_name)
            if kind is None:
                raise RuntimeMemoryCleanupError("memory_vector_collection_inventory_invalid")
            try:
                points = await list_points(
                    collection_name=collection_name,
                    tenant_id=tenant_id,
                    user_id=principal_id,
                )
            except RuntimeMemoryCleanupError:
                raise
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.memory.governance_cleanup.internal_failure",
                    exc,
                )
                raise RuntimeMemoryCleanupError("memory_vector_inventory_unavailable") from exc
            if not isinstance(points, Mapping):
                raise RuntimeMemoryCleanupError("memory_vector_inventory_invalid")
            public_points: list[dict[str, str]] = []
            for point_id, payload in points.items():
                point_id = str(point_id)
                if (
                    not isinstance(payload, Mapping)
                    or str(payload.get("tenant_id") or "") != tenant_id
                    or str(payload.get("user_id") or "") != principal_id
                ):
                    raise RuntimeMemoryCleanupError("memory_vector_scope_mismatch")
                include = cutoff is None
                if cutoff is not None:
                    payload_source_id = str(payload.get("source_id") or "")
                    if point_id in eligible_chunk_ids or payload_source_id in eligible_source_ids:
                        include = True
                    elif point_id in all_chunk_ids or payload_source_id in all_source_ids:
                        include = False
                    else:
                        indexed_at = _as_utc(
                            payload.get("indexed_at")
                            or payload.get("updated_at")
                            or payload.get("created_at")
                        )
                        if indexed_at is None:
                            raise RuntimeMemoryCleanupError("memory_vector_lineage_unknown")
                        include = indexed_at <= cutoff
                if not include:
                    continue
                try:
                    version_digest = canonical_cleanup_digest(
                        {"point_id": point_id, "payload": payload}
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeMemoryCleanupError("memory_vector_inventory_invalid") from exc
                public_points.append(
                    {
                        "point_id": point_id,
                        "version_digest": version_digest,
                    }
                )
            public_points.sort(key=lambda item: item["point_id"])
            manifests.append(
                {
                    "collection_kind": kind,
                    "collection_handle": "memvec_"
                    + hashlib.sha256(collection_name.encode()).hexdigest()[:32],
                    "_collection_name": collection_name,
                    "point_count": len(public_points),
                    "points": public_points,
                }
            )
        return manifests


__all__ = [
    "AgentRuntimeMemoryCleanupService",
    "RuntimeMemoryCleanupError",
    "RuntimeMemoryQdrantCleanupClient",
]
