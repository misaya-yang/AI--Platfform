from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from ai_gateway_core.agents import (
    build_runtime_cleanup_plan,
    canonical_cleanup_digest,
    validate_runtime_cleanup_inventory,
    validate_runtime_cleanup_receipt,
)
from ai_gateway_core.auth.gateway_secret import GatewaySecret, InMemoryReplayStore
from ai_gateway_core.auth.gateway_secret_middleware import GatewaySecretAuthMiddleware
from assistant_service.api.routes.runtime_cleanup import router as cleanup_router
from assistant_service.core.runtime.memory.governance_cleanup import (
    AgentRuntimeMemoryCleanupService,
    RuntimeMemoryCleanupError,
    RuntimeMemoryQdrantCleanupClient,
)
from assistant_service.core.runtime.memory.scope import scoped_collection_names
from assistant_service.core.runtime.memory.source_store import MemorySourceStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "tenant-a"
PRINCIPAL = "am_" + "a" * 61
AGENT_ID = "11111111-1111-1111-1111-111111111111"
DELETION_ID = "22222222-2222-2222-2222-222222222222"


def _plan(*, cutoff: datetime, tenant_id: str = TENANT) -> dict[str, Any]:
    return build_runtime_cleanup_plan(
        deletion_id=DELETION_ID,
        tenant_id=tenant_id,
        agent_id=AGENT_ID,
        scope="tenant",
        subject_user_id=None,
        cutoff_at=cutoff.isoformat(),
        principal_handles=[PRINCIPAL],
    )


class _Database:
    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}

    def add(
        self,
        *,
        path: str,
        source_id: str,
        chunk_ids: list[str],
        updated_at: datetime,
        source_type: str = "long_term",
        vector_collections: list[str] | None = None,
        content_hash: str | None = None,
        indexing_token: str | None = None,
    ) -> None:
        self.sources[path] = {
            "source_id": source_id,
            "source_path": path,
            "source_type": source_type,
            "content_hash": content_hash or "content-" + source_id,
            "created_at": updated_at,
            "updated_at": updated_at,
            "chunk_ids": list(chunk_ids),
            "metadata": {
                "vector_collections": list(vector_collections or []),
                "indexing_token": indexing_token or "index-" + source_id,
            },
        }

    async def fetch(self, _sql: str, tenant_id: str, principal_id: str) -> list[dict[str, Any]]:
        assert tenant_id == TENANT
        assert principal_id == PRINCIPAL
        rows: list[dict[str, Any]] = []
        for source in self.sources.values():
            chunk_ids = source["chunk_ids"] or [None]
            rows.extend({**source, "chunk_id": chunk_id} for chunk_id in chunk_ids)
        return rows

    def remove(self, path: str) -> None:
        self.sources.pop(path, None)


class _VectorStore:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}

    def add(self, collection: str, point_id: str, payload: dict[str, Any]) -> None:
        self.collections.setdefault(collection, {})[point_id] = dict(payload)

    async def list_collections(self) -> list[str]:
        return sorted(self.collections)

    async def list_points(
        self,
        *,
        collection_name: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, dict[str, Any]]:
        return {
            point_id: payload
            for point_id, payload in self.collections.get(collection_name, {}).items()
            if payload.get("tenant_id") == tenant_id and payload.get("user_id") == user_id
        }

    async def delete_points(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
        tenant_id: str,
        user_id: str,
    ) -> None:
        collection = self.collections.setdefault(collection_name, {})
        for point_id in point_ids:
            payload = collection.get(point_id) or {}
            if payload.get("tenant_id") == tenant_id and payload.get("user_id") == user_id:
                collection.pop(point_id, None)

    async def retrieve_vectors(
        self,
        *,
        collection_name: str,
        point_ids: list[str],
    ) -> dict[str, list[float]]:
        collection = self.collections.get(collection_name, {})
        return {point_id: [] for point_id in point_ids if point_id in collection}


class _Runtime:
    def __init__(
        self,
        *,
        store: MemorySourceStore,
        database: _Database,
        vector_store: _VectorStore | None,
    ) -> None:
        self.memory_store = store
        self.database = database
        self.memory_indexer = self
        self.vector_store = vector_store
        self.collection_prefix = "assistant_memory"
        self.expected_database_handles: list[str | None] = []

    @staticmethod
    def _database_handle(source: dict[str, Any]) -> str:
        generation = {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "source_id": str(source["source_id"]),
            "source_path": str(source["source_path"]),
            "source_type": str(source["source_type"]),
            "content_hash": str(source["content_hash"]),
            "created_at": source["created_at"].astimezone(timezone.utc).isoformat(),
            "updated_at": source["updated_at"].astimezone(timezone.utc).isoformat(),
            "chunk_ids": sorted(str(item) for item in source["chunk_ids"]),
            "vector_collections": sorted(
                str(item) for item in source.get("metadata", {}).get("vector_collections", [])
            ),
            "indexing_token": str(source.get("metadata", {}).get("indexing_token") or ""),
        }
        material = json.dumps(
            generation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"memsrc_{hashlib.sha256(material).hexdigest()[:32]}"

    async def list_scoped_source_records(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        assert tenant_id == TENANT
        assert user_id == PRINCIPAL
        return [
            {
                "source_id": source["source_id"],
                "source_path": source["source_path"],
                "source_type": source["source_type"],
                "content_hash": source["content_hash"],
                "created_at": source["created_at"],
                "updated_at": source["updated_at"],
                "chunk_ids": list(source["chunk_ids"]),
                "vector_collections": list(
                    source.get("metadata", {}).get("vector_collections", [])
                ),
                "indexing_token": str(source.get("metadata", {}).get("indexing_token") or ""),
                "source_handle": self._database_handle(source),
                "owner_proven": True,
            }
            for source in self.database.sources.values()
            if not source.get("metadata", {}).get("deletion_source_handle")
        ]

    async def inspect_memory_sources(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        inventory = self.memory_store.inspect_user_tree(tenant_id, user_id)
        sources = list(inventory["sources"])
        scoped_records = await self.list_scoped_source_records(
            tenant_id=tenant_id,
            user_id=user_id,
        )
        legacy_sources, legacy_quarantined = self.memory_store.inspect_legacy_records(
            tenant_id,
            user_id,
            scoped_records,
        )
        sources.extend(
            {key: value for key, value in source.items() if not key.startswith("_")}
            for source in legacy_sources
        )
        known_handles = {str(item["source_id"]) for item in sources}
        for source in self.database.sources.values():
            source_handle = str(source.get("metadata", {}).get("deletion_source_handle") or "")
            if source_handle and source_handle not in known_handles:
                sources.append(
                    {
                        "source_id": source_handle,
                        "label": Path(str(source["source_path"])).name,
                        "source_type": str(source["source_type"]),
                        "status": "deletion_pending",
                        "owner_proven": True,
                    }
                )
            elif (
                not source_handle
                and not Path(str(source["source_path"])).exists()
                and not any(
                    item.get("label") == Path(str(source["source_path"])).name
                    and item.get("source_type") == source["source_type"]
                    for item in sources
                )
            ):
                sources.append(
                    {
                        "source_id": self._database_handle(source),
                        "label": Path(str(source["source_path"])).name,
                        "source_type": str(source["source_type"]),
                        "status": "active",
                        "derived_only": True,
                    }
                )
        return {
            **inventory,
            "status": "ok",
            "sources": sources,
            "legacy_quarantined_sources": legacy_quarantined,
        }

    async def delete_memory_source_by_id(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_id: str,
        expected_database_source_handle: str | None = None,
    ) -> SimpleNamespace:
        assert tenant_id == TENANT
        assert user_id == PRINCIPAL
        self.expected_database_handles.append(expected_database_source_handle)
        record = self.memory_store.resolve_source_handle_record(
            tenant_id,
            user_id,
            source_id,
        )
        if record is None:
            scoped_records = await self.list_scoped_source_records(
                tenant_id=tenant_id,
                user_id=user_id,
            )
            record = self.memory_store.resolve_legacy_source_handle_record(
                tenant_id,
                user_id,
                source_id,
                scoped_records,
            )
        target = Path(str(record["_path"])) if record else None
        if target is None:
            for source in self.database.sources.values():
                if source_id in {
                    source.get("metadata", {}).get("deletion_source_handle"),
                    self._database_handle(source),
                }:
                    target = Path(str(source["source_path"]))
                    break
        assert target is not None
        index_path = str(record.get("_index_source_path") or target) if record else str(target)
        database_source = self.database.sources.get(index_path)
        if expected_database_source_handle and (
            database_source is None
            or self._database_handle(database_source) != expected_database_source_handle
        ):
            return SimpleNamespace(
                completed=False,
                source_id=source_id,
                read_back={
                    "file_absent": False,
                    "sql_source_absent": False,
                    "sql_chunks_absent": False,
                    "vector_points_remaining": None,
                },
                errors=("memory_source_generation_conflict",),
            )
        if target.exists():
            target.unlink()
        if record and record.get("_deletion_stage") == "finalizing":
            self.memory_store.clear_deletion_marker(
                tenant_id,
                user_id,
                str(target),
                source_handle=source_id,
            )
        self.database.remove(index_path)
        return SimpleNamespace(
            completed=True,
            source_id=source_id,
            read_back={
                "file_absent": True,
                "sql_source_absent": True,
                "sql_chunks_absent": True,
                "vector_points_remaining": 0,
            },
            errors=(),
        )


def _service(
    tmp_path: Path,
    *,
    vector_store: _VectorStore | None = None,
) -> tuple[AgentRuntimeMemoryCleanupService, MemorySourceStore, _Database, _VectorStore | None]:
    store = MemorySourceStore(tmp_path)
    database = _Database()
    runtime = _Runtime(store=store, database=database, vector_store=vector_store)
    return (
        AgentRuntimeMemoryCleanupService(database=database, runtime=runtime),
        store,
        database,
        vector_store,
    )


@pytest.mark.asyncio
@respx.mock
async def test_qdrant_inventory_binds_scope_and_hashes_vector_generation() -> None:
    base_url = "https://qdrant.internal"
    collection = "assistant_memory_test"
    route = respx.post(f"{base_url}/collections/{collection}/points/scroll").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": "point-1",
                                "payload": {
                                    "tenant_id": TENANT,
                                    "user_id": PRINCIPAL,
                                },
                                "vector": [0.1, 0.2],
                            }
                        ],
                        "next_page_offset": None,
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "result": {
                        "points": [
                            {
                                "id": "point-1",
                                "payload": {
                                    "tenant_id": TENANT,
                                    "user_id": PRINCIPAL,
                                },
                                "vector": [0.1, 0.3],
                            }
                        ],
                        "next_page_offset": None,
                    }
                },
            ),
        ]
    )
    client = RuntimeMemoryQdrantCleanupClient(base_url=base_url)

    first = await client.list_points(
        collection_name=collection,
        tenant_id=TENANT,
        user_id=PRINCIPAL,
    )
    second = await client.list_points(
        collection_name=collection,
        tenant_id=TENANT,
        user_id=PRINCIPAL,
    )

    assert route.call_count == 2
    request_payload = json.loads(route.calls[0].request.content)
    assert request_payload["with_vector"] is True
    assert request_payload["filter"]["must"] == [
        {"key": "tenant_id", "match": {"value": TENANT}},
        {"key": "user_id", "match": {"value": PRINCIPAL}},
    ]
    assert (
        first["point-1"]["_governance_vector_digest"]
        != second["point-1"]["_governance_vector_digest"]
    )


@pytest.mark.asyncio
async def test_cleanup_deletes_frozen_markdown_sql_and_orphan_vectors_with_readback(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(
        TENANT,
        PRINCIPAL,
        ["private fact"],
        now=old,
    )
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    chunk_id = "33333333-3333-3333-3333-333333333333"
    database.add(
        path=source_path,
        source_id="44444444-4444-4444-4444-444444444444",
        chunk_ids=[chunk_id],
        updated_at=old,
    )
    current, legacy = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)
    dimensioned = f"{current}_d1024"
    database.sources[source_path]["metadata"] = {"vector_collections": [dimensioned]}
    vectors.add(
        current,
        chunk_id,
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "source_id": "44444444-4444-4444-4444-444444444444",
        },
    )
    vectors.add(
        legacy,
        "55555555-5555-5555-5555-555555555555",
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "indexed_at": old.isoformat(),
        },
    )
    vectors.add(
        dimensioned,
        "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa",
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "indexed_at": old.isoformat(),
        },
    )

    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    assert set(inventory["principals"][0]["sources"][0]) == {
        "logical_source_scope_handle",
        "source_handle",
        "version_digest",
        "source_type",
    }
    leaked_inventory = json.loads(json.dumps(inventory))
    leaked_inventory["principals"][0]["sources"][0]["source_path"] = source_path
    leaked_inventory["inventory_digest"] = canonical_cleanup_digest(leaked_inventory)
    with pytest.raises(ValueError, match="AGENT_RUNTIME_CLEANUP_INVENTORY_INVALID"):
        validate_runtime_cleanup_inventory(leaked_inventory, plan=plan)

    drifted_inventory = json.loads(json.dumps(inventory))
    drifted_inventory["cutoff_at"] = (old - timedelta(days=1)).isoformat()
    drifted_inventory["inventory_digest"] = canonical_cleanup_digest(drifted_inventory)
    with pytest.raises(
        ValueError,
        match="AGENT_RUNTIME_CLEANUP_INVENTORY_SCOPE_MISMATCH",
    ):
        validate_runtime_cleanup_inventory(drifted_inventory, plan=plan)

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert receipt["principals"][0]["deleted_source_count"] == 1
    assert receipt["principals"][0]["deleted_vector_count"] == 3
    assert not Path(source_path).exists()
    assert database.sources == {}
    assert vectors.collections[current] == {}
    assert vectors.collections[legacy] == {}
    assert vectors.collections[dimensioned] == {}
    assert len(service.runtime.expected_database_handles) == 1
    assert str(service.runtime.expected_database_handles[0]).startswith("memsrc_")
    serialized = json.dumps({"inventory": inventory, "receipt": receipt})
    assert str(tmp_path) not in serialized
    assert "private fact" not in serialized
    assert current not in serialized
    assert dimensioned not in serialized
    assert all(
        vector_set["collection_handle"].startswith("memvec_")
        for vector_set in inventory["principals"][0]["vector_sets"]
    )

    false_complete = json.loads(json.dumps(receipt))
    false_complete["principals"][0]["deleted_vector_count"] = 0
    false_complete["receipt_digest"] = canonical_cleanup_digest(false_complete)
    with pytest.raises(ValueError, match="AGENT_RUNTIME_CLEANUP_RECEIPT_INVALID"):
        validate_runtime_cleanup_receipt(
            false_complete,
            plan=plan,
            inventory=inventory,
        )

    replay = await service.execute(plan_value=plan, inventory_value=inventory)
    assert replay["completed"] is True
    assert replay["principals"][0]["idempotent_absent_count"] == 1
    assert replay["principals"][0]["deleted_vector_count"] == 0


@pytest.mark.asyncio
async def test_cleanup_deletes_owner_proven_legacy_source_by_opaque_handle(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    store = MemorySourceStore(tmp_path / "current", legacy_base_dir=legacy_root)
    database = _Database()
    vectors = _VectorStore()
    runtime = _Runtime(store=store, database=database, vector_store=vectors)
    service = AgentRuntimeMemoryCleanupService(database=database, runtime=runtime)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    actual_path = legacy_root / TENANT / PRINCIPAL / "MEMORY.md"
    actual_path.parent.mkdir(parents=True)
    actual_path.write_text("# Legacy\n\nprivate fact\n", encoding="utf-8")
    os.utime(actual_path, (old.timestamp(), old.timestamp()))
    persisted_path = f"/old-volume/{TENANT}/{PRINCIPAL}/MEMORY.md"
    database.add(
        path=persisted_path,
        source_id="78787878-7878-4787-8787-787878787878",
        chunk_ids=[],
        updated_at=old,
    )

    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    source_handle = inventory["principals"][0]["sources"][0]["source_handle"]
    assert source_handle.startswith("memsrc_")
    assert persisted_path not in json.dumps(inventory)

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert receipt["principals"][0]["deleted_source_count"] == 1
    assert not actual_path.exists()
    assert database.sources == {}


@pytest.mark.asyncio
async def test_prepare_cutoff_preserves_new_source_and_vector(tmp_path: Path) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    cutoff = datetime.now(timezone.utc)
    new_time = cutoff + timedelta(seconds=1)
    new_write = store.append_profile_facts(
        TENANT,
        PRINCIPAL,
        ["new fact"],
        now=new_time,
    )
    os.utime(new_write.path, (new_time.timestamp(), new_time.timestamp()))
    new_chunk = "66666666-6666-6666-6666-666666666666"
    database.add(
        path=new_write.path,
        source_id="77777777-7777-7777-7777-777777777777",
        chunk_ids=[new_chunk],
        updated_at=new_time,
        source_type="profile",
    )
    current = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)[0]
    vectors.add(
        current,
        new_chunk,
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "source_id": "77777777-7777-7777-7777-777777777777",
            "indexed_at": new_time.isoformat(),
        },
    )

    plan = _plan(cutoff=cutoff)
    inventory = await service.inspect(plan)
    assert inventory["source_count"] == 0
    assert inventory["vector_count"] == 0
    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert Path(new_write.path).exists()
    assert new_write.path in database.sources
    assert new_chunk in vectors.collections[current]


@pytest.mark.asyncio
async def test_cleanup_includes_sql_only_generation_and_derived_vector(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(
        TENANT,
        PRINCIPAL,
        ["source later lost"],
        now=old,
    )
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    chunk_id = "12121212-1212-4212-8212-121212121212"
    database.add(
        path=source_path,
        source_id="34343434-3434-4434-8434-343434343434",
        chunk_ids=[chunk_id],
        updated_at=old,
    )
    Path(source_path).unlink()
    current = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)[0]
    vectors.add(
        current,
        chunk_id,
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "source_id": "34343434-3434-4434-8434-343434343434",
        },
    )

    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    assert inventory["source_count"] == 1
    assert inventory["vector_count"] == 1
    assert str(tmp_path) not in json.dumps(inventory)

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert receipt["principals"][0]["deleted_source_count"] == 1
    assert receipt["principals"][0]["deleted_vector_count"] == 1
    assert database.sources == {}
    assert vectors.collections[current] == {}


@pytest.mark.asyncio
async def test_changed_frozen_source_returns_partial_without_overdelete(tmp_path: Path) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(TENANT, PRINCIPAL, ["old"], now=old)
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    database.add(
        path=source_path,
        source_id="88888888-8888-8888-8888-888888888888",
        chunk_ids=[],
        updated_at=old,
    )
    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)

    Path(source_path).write_text("new content", encoding="utf-8")
    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["status"] == "partial"
    assert receipt["retryable"] is True
    assert receipt["errors"] == ["memory_source_changed_since_prepare"]
    assert Path(source_path).read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_sql_reindex_after_inspect_changes_frozen_source_version(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(TENANT, PRINCIPAL, ["old"], now=old)
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    old_chunk = "81818181-8181-4181-8181-818181818181"
    new_chunk = "82828282-8282-4282-8282-828282828282"
    source_id = "83838383-8383-4383-8383-838383838383"
    database.add(
        path=source_path,
        source_id=source_id,
        chunk_ids=[old_chunk],
        updated_at=old,
    )
    collection = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)[0]
    vectors.add(
        collection,
        old_chunk,
        {"tenant_id": TENANT, "user_id": PRINCIPAL, "source_id": source_id},
    )
    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)

    reindexed_at = old + timedelta(hours=1)
    database.sources[source_path].update(
        {
            "content_hash": "new-sql-generation",
            "updated_at": reindexed_at,
            "chunk_ids": [new_chunk],
        }
    )
    database.sources[source_path]["metadata"]["indexing_token"] = "index-new"
    vectors.add(
        collection,
        new_chunk,
        {"tenant_id": TENANT, "user_id": PRINCIPAL, "source_id": source_id},
    )

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["status"] == "partial"
    assert receipt["errors"] == ["memory_source_changed_since_prepare"]
    assert Path(source_path).is_file()
    assert database.sources[source_path]["chunk_ids"] == [new_chunk]
    assert set(vectors.collections[collection]) == {old_chunk, new_chunk}
    assert service.runtime.expected_database_handles == []


@pytest.mark.asyncio
async def test_post_check_reindex_is_stopped_by_database_generation_fence(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(TENANT, PRINCIPAL, ["old"], now=old)
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    reused_chunk = "84848484-8484-4484-8484-848484848484"
    new_chunk = "85858585-8585-4585-8585-858585858585"
    source_id = "86868686-8686-4686-8686-868686868686"
    database.add(
        path=source_path,
        source_id=source_id,
        chunk_ids=[reused_chunk],
        updated_at=old,
    )
    collection = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)[0]
    vectors.add(
        collection,
        reused_chunk,
        {
            "tenant_id": TENANT,
            "user_id": PRINCIPAL,
            "source_id": source_id,
            "generation": "old",
        },
    )
    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    original_delete = service.runtime.delete_memory_source_by_id

    async def reindex_then_delete(
        *,
        tenant_id: str,
        user_id: str,
        source_id: str,
        expected_database_source_handle: str | None = None,
    ) -> SimpleNamespace:
        database.sources[source_path].update(
            {
                "content_hash": "raced-sql-generation",
                "updated_at": datetime.now(timezone.utc),
                "chunk_ids": [reused_chunk, new_chunk],
            }
        )
        database.sources[source_path]["metadata"]["indexing_token"] = "index-raced"
        vectors.add(
            collection,
            reused_chunk,
            {
                "tenant_id": TENANT,
                "user_id": PRINCIPAL,
                "source_id": source_id,
                "generation": "new",
            },
        )
        vectors.add(
            collection,
            new_chunk,
            {
                "tenant_id": TENANT,
                "user_id": PRINCIPAL,
                "source_id": source_id,
                "generation": "new",
            },
        )
        return await original_delete(
            tenant_id=tenant_id,
            user_id=user_id,
            source_id=source_id,
            expected_database_source_handle=expected_database_source_handle,
        )

    service.runtime.delete_memory_source_by_id = reindex_then_delete
    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["status"] == "partial"
    assert "memory_source_generation_conflict" in receipt["errors"]
    assert Path(source_path).is_file()
    assert database.sources[source_path]["chunk_ids"] == [reused_chunk, new_chunk]
    assert vectors.collections[collection][reused_chunk]["generation"] == "new"
    assert vectors.collections[collection][new_chunk]["generation"] == "new"
    assert service.runtime.expected_database_handles[0] is not None


@pytest.mark.asyncio
async def test_retry_resumes_persisted_generation_after_file_unlink_crash(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(
        TENANT,
        PRINCIPAL,
        ["old generation"],
        now=old,
    )
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    database.add(
        path=source_path,
        source_id="bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb",
        chunk_ids=[],
        updated_at=old,
    )
    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    frozen_handle = inventory["principals"][0]["sources"][0]["source_handle"]

    Path(source_path).unlink()
    database.sources[source_path]["metadata"] = {
        "deletion_source_handle": frozen_handle,
        "vector_collections": [],
    }
    database.sources[source_path]["updated_at"] = datetime.now(timezone.utc)

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert receipt["principals"][0]["deleted_source_count"] == 1
    assert database.sources == {}


@pytest.mark.asyncio
async def test_retry_accepts_finalizing_marker_for_same_frozen_generation(
    tmp_path: Path,
) -> None:
    vectors = _VectorStore()
    service, store, database, _ = _service(tmp_path, vector_store=vectors)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    source_path = store.append_long_term_facts(
        TENANT,
        PRINCIPAL,
        ["generation awaiting marker cleanup"],
        now=old,
    )
    os.utime(source_path, (old.timestamp(), old.timestamp()))
    database.add(
        path=source_path,
        source_id="56565656-5656-4656-8656-565656565656",
        chunk_ids=[],
        updated_at=old,
    )
    plan = _plan(cutoff=datetime.now(timezone.utc))
    inventory = await service.inspect(plan)
    frozen_handle = inventory["principals"][0]["sources"][0]["source_handle"]

    stage_status, _ = store.stage_source_for_deletion(
        TENANT,
        PRINCIPAL,
        source_path,
        expected_source_handle=frozen_handle,
    )
    assert stage_status == "staged"
    assert (
        store.delete_staged_source(
            TENANT,
            PRINCIPAL,
            source_path,
            expected_source_handle=frozen_handle,
        )
        == "deleted"
    )
    database.remove(source_path)
    finalizing = store._finalizing_source_path(Path(source_path), frozen_handle)
    assert finalizing.exists()

    receipt = await service.execute(plan_value=plan, inventory_value=inventory)

    assert receipt["completed"] is True
    assert receipt["principals"][0]["deleted_source_count"] == 1
    assert not finalizing.exists()


@pytest.mark.asyncio
async def test_vector_inventory_is_fail_closed_without_provider_or_lineage(
    tmp_path: Path,
) -> None:
    service, _, _, _ = _service(tmp_path, vector_store=None)
    with pytest.raises(RuntimeMemoryCleanupError, match="memory_vector_inventory_unavailable"):
        await service.inspect(_plan(cutoff=datetime.now(timezone.utc)))

    point_only_store = SimpleNamespace(
        list_points=lambda **_kwargs: {},
    )
    service, _, _, _ = _service(
        tmp_path / "no-collection-inventory",
        vector_store=point_only_store,
    )
    with pytest.raises(
        RuntimeMemoryCleanupError,
        match="memory_vector_collection_inventory_unavailable",
    ):
        await service.inspect(_plan(cutoff=datetime.now(timezone.utc)))

    vectors = _VectorStore()
    service, _, _, _ = _service(tmp_path / "orphan", vector_store=vectors)
    current = scoped_collection_names("assistant_memory", TENANT, PRINCIPAL)[0]
    vectors.add(
        current,
        "99999999-9999-9999-9999-999999999999",
        {"tenant_id": TENANT, "user_id": PRINCIPAL},
    )
    with pytest.raises(RuntimeMemoryCleanupError, match="memory_vector_lineage_unknown"):
        await service.inspect(_plan(cutoff=datetime.now(timezone.utc)))


class _RouteService:
    async def inspect(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {"tenant_id": plan["tenant_id"]}


def _signed_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, GatewaySecret]:
    monkeypatch.setenv("INTERNAL_AUTH_VERSION", "v2")
    secret = "test-shared-secret-value"
    verifier = GatewaySecret(
        secret=secret,
        version="v2",
        replay_store=InMemoryReplayStore(),
    )
    signer = GatewaySecret(
        secret=secret,
        version="v2",
        replay_store=InMemoryReplayStore(),
    )
    app = FastAPI()
    app.include_router(cleanup_router, prefix="/api/v1/assistant")
    app.state.runtime_memory_cleanup_service = _RouteService()
    app.add_middleware(
        GatewaySecretAuthMiddleware,
        gateway_secret=verifier,
        allow_anonymous=True,
    )
    return TestClient(app), signer


def _signed_headers(
    signer: GatewaySecret,
    *,
    path: str,
    body: bytes,
    tenant_id: str,
) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-User-Id": "agent-governance-cleanup",
        "X-Tenant-Id": tenant_id,
        "X-User-Type": "system",
        "X-User-Roles": "admin",
        signer.header_name: signer.sign(
            method="POST",
            path=path,
            query="",
            body=body,
        ),
    }


def test_internal_route_requires_verified_hmac_and_exact_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, signer = _signed_client(monkeypatch)
    path = "/api/v1/assistant/internal/runtime-memory-cleanup/inventory"
    plan = _plan(cutoff=datetime.now(timezone.utc), tenant_id="tenant-b")
    body = json.dumps(
        {"plan": plan},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    unsigned = client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-User-Id": "agent-governance-cleanup",
            "X-Tenant-Id": "tenant-b",
            "X-User-Type": "system",
            "X-User-Roles": "admin",
        },
    )
    assert unsigned.status_code == 401

    mismatch_headers = _signed_headers(
        signer,
        path=path,
        body=body,
        tenant_id="tenant-a",
    )
    mismatch = client.post(path, content=body, headers=mismatch_headers)
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"]["code"] == "AGENT_RUNTIME_CLEANUP_TENANT_MISMATCH"

    valid_headers = _signed_headers(
        signer,
        path=path,
        body=body,
        tenant_id="tenant-b",
    )
    valid = client.post(path, content=body, headers=valid_headers)
    assert valid.status_code == 200
    replay = client.post(path, content=body, headers=valid_headers)
    assert replay.status_code == 401
