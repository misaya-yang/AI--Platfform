from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from ai_gateway_core.agents import (
    RUNTIME_CLEANUP_INVENTORY_SCHEMA,
    RUNTIME_CLEANUP_RECEIPT_SCHEMA,
    agent_memory_principal,
    build_runtime_cleanup_plan,
    canonical_cleanup_digest,
)
from ai_gateway_core.auth.gateway_secret import GatewaySecret, InMemoryReplayStore
from ai_gateway_core.persistence.repositories.agent_repository import (
    AgentRepositoryError,
    DatabaseAgentRepository,
)
from ai_gateway_core.storage.file_storage import FileStorageService
from ai_gateway_core.storage.image_storage import OSSStorageBackend, S3StorageBackend
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import get_user_context
from src.api.v1 import agents as agents_api
from src.core.auth.user_resolver import UserContext
from src.services.agent_runtime_cleanup import (
    AgentRuntimeCleanupClient,
    AgentRuntimeCleanupClientError,
)

AGENT_ID = str(uuid.uuid4())


def _user() -> UserContext:
    return UserContext(
        user_id="owner-a",
        tenant_id="tenant-a",
        tier="normal",
        is_authenticated=True,
        roles=["user"],
        ip="127.0.0.1",
    )


class _Repository:
    def __init__(self) -> None:
        self.finished: bool | None = None
        self.prepared: dict[str, Any] | None = None
        self.events: list[str] = []

    async def list_agents(self, **_: Any) -> dict[str, Any]:
        return {"items": [], "next_cursor": None}

    async def prepare_agent_data_deletion(self, **kwargs: Any) -> dict[str, Any]:
        deletion_id = str(uuid.uuid4())
        plan = build_runtime_cleanup_plan(
            deletion_id=deletion_id,
            tenant_id=kwargs["tenant_id"],
            agent_id=kwargs["agent_id"],
            scope=kwargs["scope"],
            subject_user_id=kwargs["subject_user_id"],
            cutoff_at=datetime.now(timezone.utc).isoformat(),
            principal_handles=[],
        )
        self.prepared = {
            "deletion_id": deletion_id,
            "tenant_id": kwargs["tenant_id"],
            "agent_id": kwargs["agent_id"],
            "scope": kwargs["scope"],
            "subject_user_id": kwargs["subject_user_id"],
            "status": "pending",
            "object_keys": ["tenant-a/opaque-object"],
            "deleted_counts": {"runtime_cleanup_plan": plan},
            "error_code": None,
            "requested_by": kwargs["user_id"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }
        return dict(self.prepared)

    @asynccontextmanager
    async def claim_agent_data_deletion_execution(self, **_: Any) -> AsyncIterator[dict[str, Any]]:
        assert self.prepared is not None

        async def guard() -> None:
            self.events.append("fence_verified")

        async def freeze_inventory(*, inventory: dict[str, Any]) -> dict[str, Any]:
            await guard()
            return await self.freeze_agent_runtime_cleanup_inventory(inventory=inventory)

        async def finish(
            *,
            storage_cleanup_succeeded: bool,
            runtime_cleanup_receipt: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            await guard()
            return await self.finish_agent_data_deletion(
                tenant_id=self.prepared["tenant_id"],
                agent_id=self.prepared["agent_id"],
                deletion_id=self.prepared["deletion_id"],
                user_id=self.prepared["requested_by"],
                storage_cleanup_succeeded=storage_cleanup_succeeded,
                runtime_cleanup_receipt=runtime_cleanup_receipt,
            )

        claimed = dict(self.prepared)
        claimed.update(
            {
                "execution_claimed": True,
                "_execution_claim_token": "test-claim-token",
                "_execution_generation": 1,
                "_execution_guard": guard,
                "_execution_freeze_inventory": freeze_inventory,
                "_execution_finish": finish,
            }
        )
        self.events.append("execution_claimed")
        yield claimed

    async def freeze_agent_runtime_cleanup_inventory(self, **kwargs: Any) -> dict[str, Any]:
        assert self.prepared is not None
        prepared = dict(self.prepared)
        prepared["deleted_counts"] = {
            "runtime_cleanup_plan": kwargs["inventory"]["_plan"],
            "runtime_cleanup_inventory": {
                key: value for key, value in kwargs["inventory"].items() if key != "_plan"
            },
        }
        return prepared

    async def finish_agent_data_deletion(self, **kwargs: Any) -> dict[str, Any]:
        runtime_receipt = kwargs.get("runtime_cleanup_receipt") or {}
        runtime_ok = runtime_receipt.get("completed") is True
        self.finished = kwargs["storage_cleanup_succeeded"] and runtime_ok
        now = datetime.now(timezone.utc).isoformat()
        error_code = None
        if not kwargs["storage_cleanup_succeeded"]:
            error_code = "AGENT_STORAGE_CLEANUP_FAILED"
        elif not runtime_ok:
            error_code = "AGENT_RUNTIME_CLEANUP_FAILED"
        return {
            "deletion_id": kwargs["deletion_id"],
            "tenant_id": kwargs["tenant_id"],
            "agent_id": kwargs["agent_id"],
            "scope": "tenant",
            "subject_user_id": None,
            "status": "completed" if self.finished else "failed",
            "deleted_counts": {},
            "error_code": error_code,
            "requested_by": kwargs["user_id"],
            "requested_at": now,
            "completed_at": now if self.finished else None,
        }


class _ClaimConnection:
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        deletion_id = str(uuid.uuid4())
        self.lock_held = False
        self.policy: dict[str, Any] | None = None
        self.request: dict[str, Any] = {
            "deletion_id": deletion_id,
            "tenant_id": "tenant-a",
            "agent_id": AGENT_ID,
            "scope": "retention",
            "subject_user_id": None,
            "status": "pending",
            "object_keys": [],
            "deleted_counts": {
                "policy": {},
                "runtime_cleanup_plan": build_runtime_cleanup_plan(
                    deletion_id=deletion_id,
                    tenant_id="tenant-a",
                    agent_id=AGENT_ID,
                    scope="retention",
                    subject_user_id=None,
                    cutoff_at=now.isoformat(),
                    principal_handles=[],
                ),
            },
            "error_code": None,
            "requested_by": "owner-a",
            "requested_at": now,
            "attempt_count": 0,
            "last_attempt_at": None,
            "completed_at": None,
        }

    def transaction(self) -> _ClaimConnection:
        return self

    async def __aenter__(self) -> _ClaimConnection:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def is_closed(self) -> bool:
        return False

    async def fetchval(self, sql: str, *_: Any) -> Any:
        if "pg_try_advisory_lock" in sql:
            assert self.lock_held is False
            self.lock_held = True
            return True
        if "pg_advisory_unlock" in sql:
            assert self.lock_held is True
            self.lock_held = False
            return True
        if "SELECT NOW()" in sql:
            return datetime.now(timezone.utc)
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM agents a" in sql:
            return {
                "agent_id": AGENT_ID,
                "tenant_id": "tenant-a",
                "deleted_at": None,
                "caller_role": "owner",
            }
        if "SELECT legal_hold" in sql:
            return {"legal_hold": bool((self.policy or {}).get("legal_hold"))}
        if "SELECT * FROM agent_governance_policies" in sql:
            return dict(self.policy) if self.policy is not None else None
        if "INSERT INTO agent_governance_policies" in sql:
            self.policy = {
                "tenant_id": args[0],
                "agent_id": args[1],
                "trace_retention_days": args[2],
                "runtime_retention_days": args[3],
                "attachment_retention_days": args[4],
                "legal_hold": args[5],
                "principal_requests_per_minute": args[6],
                "principal_requests_per_day": args[7],
                "ip_requests_per_minute": args[8],
                "ip_requests_per_day": args[9],
                "publication_requests_per_minute": args[10],
                "publication_requests_per_day": args[11],
                "alert_threshold_percent": args[12],
                "max_agents_per_tenant": args[13],
                "max_active_publications": args[14],
                "max_concurrent_runs": args[15],
                "max_daily_tokens": args[16],
                "max_daily_mcp_calls": args[17],
                "max_storage_bytes": args[18],
                "updated_by": args[19],
            }
            return dict(self.policy)
        if "SELECT status, deleted_counts" in sql:
            return {
                "status": self.request["status"],
                "deleted_counts": self.request["deleted_counts"],
            }
        if "SELECT * FROM agent_data_deletion_requests" in sql:
            return dict(self.request)
        if "SET status = 'pending'" in sql:
            self.request.update(
                {
                    "status": "pending",
                    "deleted_counts": json.loads(args[1]),
                    "error_code": "AGENT_DATA_DELETION_EXECUTION_IN_PROGRESS",
                    "attempt_count": int(self.request["attempt_count"]) + 1,
                    "last_attempt_at": datetime.now(timezone.utc),
                    "completed_at": None,
                }
            )
            return dict(self.request)
        if "SET deleted_counts = $2::jsonb" in sql:
            self.request["deleted_counts"] = json.loads(args[1])
            return dict(self.request)
        if "AGENT_STORAGE_CLEANUP_FAILED" in sql:
            self.request.update(
                {
                    "status": "failed",
                    "deleted_counts": json.loads(args[1]),
                    "error_code": "AGENT_STORAGE_CLEANUP_FAILED",
                    "completed_at": None,
                }
            )
            return dict(self.request)
        raise AssertionError(sql)

    async def fetch(self, sql: str, *_: Any) -> list[dict[str, Any]]:
        if "FROM agent_data_deletion_requests" in sql and "status IN" in sql:
            if self.request["status"] in {"pending", "failed"}:
                return [dict(self.request)]
            return []
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        if "UPDATE agent_data_deletion_requests" in sql:
            self.request.update(
                {
                    "status": "blocked",
                    "error_code": args[1],
                    "deleted_counts": json.loads(args[2]),
                    "completed_at": datetime.now(timezone.utc),
                }
            )
            return "UPDATE 1"
        assert "INSERT INTO audit_logs" in sql
        return "INSERT 0 1"


class _ClaimPool:
    def __init__(self, conn: _ClaimConnection) -> None:
        self.conn = conn

    def acquire(self) -> _ClaimConnection:
        return self.conn


class _StrictSingleConnectionLease:
    def __init__(self, pool: _StrictSingleConnectionPool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _ClaimConnection:
        if self.pool.active:
            self.pool.waiter_seen.set()
        await self.pool.slot.acquire()
        self.pool.active += 1
        self.pool.max_active = max(self.pool.max_active, self.pool.active)
        return self.pool.conn

    async def __aexit__(self, *_: Any) -> None:
        self.pool.active -= 1
        self.pool.slot.release()


class _StrictSingleConnectionPool:
    """A one-slot pool that makes accidental nested acquisition observable."""

    def __init__(self, conn: _ClaimConnection) -> None:
        self.conn = conn
        self.slot = asyncio.Semaphore(1)
        self.acquire_calls = 0
        self.active = 0
        self.max_active = 0
        self.waiter_seen = asyncio.Event()

    def acquire(self) -> _StrictSingleConnectionLease:
        self.acquire_calls += 1
        return _StrictSingleConnectionLease(self)


class _FailingStorage:
    async def delete_file(self, _: str) -> bool:
        return False


class _SuccessfulStorage:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    async def delete_file(self, _: str) -> bool:
        if self.events is not None:
            assert self.events[-1] == "fence_verified"
            self.events.append("storage_delete")
        return True

    async def file_exists(self, _: str) -> bool:
        if self.events is not None:
            assert self.events[-1] == "fence_verified"
            self.events.append("storage_readback")
        return False


class _ReadbackPresentStorage:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def delete_file(self, _: str) -> bool:
        assert self.events[-1] == "fence_verified"
        self.events.append("storage_delete")
        return True

    async def file_exists(self, _: str) -> bool:
        assert self.events[-1] == "fence_verified"
        self.events.append("storage_readback_present")
        return True


class _ExceptionalStorage:
    def __init__(self, events: list[str], failure_at: str) -> None:
        self.events = events
        self.failure_at = failure_at

    async def delete_file(self, _: str) -> bool:
        assert self.events[-1] == "fence_verified"
        self.events.append(
            "storage_delete_error" if self.failure_at == "delete" else "storage_delete"
        )
        if self.failure_at == "delete":
            raise RuntimeError("tenant-a/opaque-object/delete-secret")
        return True

    async def file_exists(self, _: str) -> bool:
        assert self.events[-1] == "fence_verified"
        self.events.append(
            "storage_readback_error" if self.failure_at == "readback" else "storage_readback"
        )
        if self.failure_at == "readback":
            raise RuntimeError("tenant-a/opaque-object/readback-secret")
        return False


class _DeleteBackendSuccess:
    async def delete(self, _: str) -> bool:
        return True


class _DeleteClientFailure:
    async def delete_object(self, **_: Any) -> None:
        raise RuntimeError("raw-provider-secret")

    async def head_object(self, **_: Any) -> None:
        raise RuntimeError("raw-readback-secret")


class _DeleteBucketFailure:
    def delete_object(self, _: str) -> None:
        raise RuntimeError("raw-provider-secret")


class _CleanupClient:
    def __init__(
        self,
        *,
        partial: bool = False,
        unavailable: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.partial = partial
        self.unavailable = unavailable
        self.events = events

    async def inspect(self, plan: dict[str, Any]) -> dict[str, Any]:
        inventory: dict[str, Any] = {
            "schema_version": RUNTIME_CLEANUP_INVENTORY_SCHEMA,
            "deletion_id": plan["deletion_id"],
            "tenant_id": plan["tenant_id"],
            "agent_id": plan["agent_id"],
            "plan_digest": plan["plan_digest"],
            "cutoff_at": plan["cutoff_at"],
            "principal_count": 0,
            "source_count": 0,
            "vector_count": 0,
            "principals": [],
        }
        inventory["inventory_digest"] = canonical_cleanup_digest(inventory)
        return {**inventory, "_plan": plan}

    async def execute(
        self,
        *,
        plan_value: dict[str, Any],
        inventory_value: dict[str, Any],
    ) -> dict[str, Any]:
        if self.events is not None:
            assert self.events[-1] == "fence_verified"
            self.events.append("runtime_delete")
        if self.unavailable:
            raise AgentRuntimeCleanupClientError("AGENT_RUNTIME_CLEANUP_UPSTREAM_UNAVAILABLE")
        completed = not self.partial
        receipt: dict[str, Any] = {
            "schema_version": RUNTIME_CLEANUP_RECEIPT_SCHEMA,
            "deletion_id": plan_value["deletion_id"],
            "tenant_id": plan_value["tenant_id"],
            "agent_id": plan_value["agent_id"],
            "plan_digest": plan_value["plan_digest"],
            "inventory_digest": inventory_value["inventory_digest"],
            "status": "completed" if completed else "partial",
            "completed": completed,
            "retryable": not completed,
            "principals": [],
            "errors": [] if completed else ["memory_vector_delete_failed"],
        }
        receipt["receipt_digest"] = canonical_cleanup_digest(receipt)
        return receipt


def _client(
    repository: _Repository,
    cleanup_client: _CleanupClient | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1")
    app.state.agent_repository = repository
    app.state.agent_runtime_cleanup_client = cleanup_client or _CleanupClient(
        events=repository.events
    )
    app.dependency_overrides[get_user_context] = _user
    return TestClient(app)


@pytest.mark.asyncio
async def test_retention_principal_inventory_uses_frozen_session_update_cutoff() -> None:
    cutoff = datetime.now(timezone.utc)
    version_id = str(uuid.uuid4())

    class _PrincipalConnection:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            assert "FROM sessions" in sql
            assert "updated_at <= $3" in sql
            assert args == ("tenant-a", AGENT_ID, cutoff)
            return [
                {
                    "user_id": "subject-a",
                    "agent_version_id": version_id,
                    "agent_draft_revision": None,
                }
            ]

    repository = DatabaseAgentRepository(SimpleNamespace(_pool=None, enabled=True))
    principals = await repository._frozen_agent_memory_principals(
        _PrincipalConnection(),
        tenant_id="tenant-a",
        agent_id=AGENT_ID,
        scope="retention",
        subject_user_id=None,
        cutoff_at=cutoff,
    )

    assert (
        agent_memory_principal(
            "subject-a",
            AGENT_ID,
            f"version:{version_id}",
        )
        in principals
    )
    assert len(principals) == 2


@pytest.mark.asyncio
async def test_crash_recovery_rotates_claim_and_rejects_stale_generation() -> None:
    conn = _ClaimConnection()
    repository = DatabaseAgentRepository(SimpleNamespace(_pool=_ClaimPool(conn), enabled=True))
    common = {
        "tenant_id": "tenant-a",
        "agent_id": AGENT_ID,
        "deletion_id": conn.request["deletion_id"],
        "user_id": "owner-a",
        "is_tenant_admin": False,
    }

    async with repository.claim_agent_data_deletion_execution(**common) as first:
        assert first["execution_claimed"] is True
        await first["_execution_guard"]()
        first_token = first["_execution_claim_token"]
        first_generation = first["_execution_generation"]
        assert first_generation == 1
        assert first_token not in json.dumps(conn.request, default=str)
    assert conn.lock_held is False

    with pytest.raises(AgentRepositoryError, match="AGENT_LEGAL_HOLD_CLEANUP_ACTIVE"):
        await repository.update_governance_policy(
            tenant_id="tenant-a",
            agent_id=AGENT_ID,
            user_id="owner-a",
            is_tenant_admin=False,
            changes={"legal_hold": True},
        )
    assert conn.policy is None
    assert conn.request["deleted_counts"]["cleanup_execution"]["state"] == "claimed"

    async with repository.claim_agent_data_deletion_execution(**common) as recovered:
        assert recovered["execution_claimed"] is True
        assert recovered["_execution_generation"] == 2
        with pytest.raises(
            AgentRepositoryError,
            match="AGENT_DATA_DELETION_EXECUTION_CLAIM_INVALID",
        ):
            await repository.finish_agent_data_deletion(
                **common,
                storage_cleanup_succeeded=False,
                execution_claim_token=first_token,
                execution_generation=first_generation,
                _execution_connection=conn,
            )
        failed = await recovered["_execution_finish"](
            storage_cleanup_succeeded=False,
        )
        assert failed["status"] == "failed"
        assert failed["attempt_count"] == 2
        assert failed["deleted_counts"]["cleanup_execution"]["state"] == "retryable"

    assert conn.lock_held is False

    applied = await repository.update_governance_policy(
        tenant_id="tenant-a",
        agent_id=AGENT_ID,
        user_id="owner-a",
        is_tenant_admin=False,
        changes={"legal_hold": True},
    )
    assert applied["legal_hold"] is True
    assert conn.request["status"] == "blocked"


def _inventory_for_claim(claimed: dict[str, Any]) -> dict[str, Any]:
    counts = claimed["deleted_counts"]
    plan = counts["runtime_cleanup_plan"]
    inventory: dict[str, Any] = {
        "schema_version": RUNTIME_CLEANUP_INVENTORY_SCHEMA,
        "deletion_id": plan["deletion_id"],
        "tenant_id": plan["tenant_id"],
        "agent_id": plan["agent_id"],
        "plan_digest": plan["plan_digest"],
        "cutoff_at": plan["cutoff_at"],
        "principal_count": 0,
        "source_count": 0,
        "vector_count": 0,
        "principals": [],
    }
    inventory["inventory_digest"] = canonical_cleanup_digest(inventory)
    return inventory


@pytest.mark.asyncio
async def test_claim_freeze_and_finish_reuse_one_pool_connection() -> None:
    conn = _ClaimConnection()
    pool = _StrictSingleConnectionPool(conn)
    repository = DatabaseAgentRepository(SimpleNamespace(_pool=pool, enabled=True))
    common = {
        "tenant_id": "tenant-a",
        "agent_id": AGENT_ID,
        "deletion_id": conn.request["deletion_id"],
        "user_id": "owner-a",
        "is_tenant_admin": False,
    }

    async with repository.claim_agent_data_deletion_execution(**common) as claimed:
        assert claimed["execution_claimed"] is True
        assert pool.acquire_calls == 1
        assert "_execution_connection" not in claimed
        claim_token = claimed["_execution_claim_token"]

        frozen = await asyncio.wait_for(
            claimed["_execution_freeze_inventory"](
                inventory=_inventory_for_claim(claimed),
            ),
            timeout=1.0,
        )
        assert frozen["deleted_counts"]["runtime_cleanup_inventory"]["principal_count"] == 0
        finished = await asyncio.wait_for(
            claimed["_execution_finish"](storage_cleanup_succeeded=False),
            timeout=1.0,
        )

        assert finished["status"] == "failed"
        assert finished["deleted_counts"]["cleanup_execution"]["state"] == "retryable"
        assert pool.acquire_calls == 1
        public_claim = {
            key: value for key, value in claimed.items() if not key.startswith("_execution_")
        }
        assert claim_token not in json.dumps(public_claim, default=str)

    assert pool.active == 0
    assert pool.max_active == 1
    assert conn.lock_held is False


@pytest.mark.asyncio
async def test_saturated_single_connection_pool_does_not_deadlock_claim_callbacks() -> None:
    conn = _ClaimConnection()
    pool = _StrictSingleConnectionPool(conn)
    repository = DatabaseAgentRepository(SimpleNamespace(_pool=pool, enabled=True))
    common = {
        "tenant_id": "tenant-a",
        "agent_id": AGENT_ID,
        "deletion_id": conn.request["deletion_id"],
        "user_id": "owner-a",
        "is_tenant_admin": False,
    }
    first_claimed = asyncio.Event()

    async def execute_cleanup(*, wait_for_competitor: bool) -> int:
        async with repository.claim_agent_data_deletion_execution(**common) as claimed:
            assert claimed["execution_claimed"] is True
            generation = int(claimed["_execution_generation"])
            if wait_for_competitor:
                first_claimed.set()
                await asyncio.wait_for(pool.waiter_seen.wait(), timeout=1.0)
            await claimed["_execution_freeze_inventory"](
                inventory=_inventory_for_claim(claimed),
            )
            finished = await claimed["_execution_finish"](
                storage_cleanup_succeeded=False,
            )
            assert finished["status"] == "failed"
            return generation

    first = asyncio.create_task(execute_cleanup(wait_for_competitor=True))
    await asyncio.wait_for(first_claimed.wait(), timeout=1.0)
    second = asyncio.create_task(execute_cleanup(wait_for_competitor=False))
    generations = await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)

    assert generations == [1, 2]
    assert pool.acquire_calls == 2
    assert pool.max_active == 1
    assert pool.active == 0
    assert conn.lock_held is False


@pytest.mark.asyncio
@respx.mock
async def test_cleanup_client_binds_canonical_body_and_path_to_v2_hmac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "governance-cleanup-test-secret"
    base_url = "https://assistant.internal"
    path = "/api/v1/assistant/internal/runtime-memory-cleanup/inventory"
    monkeypatch.setenv("ASSISTANT_SERVICE_URL", base_url)
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", secret)
    plan = build_runtime_cleanup_plan(
        deletion_id=str(uuid.uuid4()),
        tenant_id="tenant-a",
        agent_id=AGENT_ID,
        scope="tenant",
        subject_user_id=None,
        cutoff_at=datetime.now(timezone.utc).isoformat(),
        principal_handles=[],
    )
    inventory: dict[str, Any] = {
        "schema_version": RUNTIME_CLEANUP_INVENTORY_SCHEMA,
        "deletion_id": plan["deletion_id"],
        "tenant_id": plan["tenant_id"],
        "agent_id": plan["agent_id"],
        "plan_digest": plan["plan_digest"],
        "cutoff_at": plan["cutoff_at"],
        "principal_count": 0,
        "source_count": 0,
        "vector_count": 0,
        "principals": [],
    }
    inventory["inventory_digest"] = canonical_cleanup_digest(inventory)
    verifier = GatewaySecret(
        secret=secret,
        version="v2",
        replay_store=InMemoryReplayStore(),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        expected_body = AgentRuntimeCleanupClient._encode({"plan": plan})
        assert request.content == expected_body
        signature = request.headers[verifier.header_name]
        assert signature.startswith("v2:")
        verifier.verify(
            signature,
            method="POST",
            path=path,
            query="",
            body=request.content,
        )
        return httpx.Response(200, json=inventory)

    route = respx.post(base_url + path).mock(side_effect=respond)
    result = await AgentRuntimeCleanupClient().inspect(plan)

    assert route.called
    assert result["inventory_digest"] == inventory["inventory_digest"]


def test_management_flag_is_read_only_and_preserves_non_agent_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1")

    @app.get("/api/v1/assistant/health")
    async def assistant_health() -> dict[str, bool]:
        return {"ok": True}

    app.state.agent_repository = repository
    app.dependency_overrides[get_user_context] = _user
    monkeypatch.setenv("AGENT_STUDIO_MANAGEMENT_ENABLED", "false")
    client = TestClient(app)

    assert client.get("/api/v1/agents").status_code == 200
    denied = client.post(
        "/api/v1/agents",
        json={"name": "blocked", "description": "", "spec": {}},
    )
    assert denied.status_code == 503
    assert denied.json()["detail"]["code"] == "AGENT_STUDIO_MUTATIONS_DISABLED"
    assert client.get("/api/v1/assistant/health").json() == {"ok": True}


def test_storage_failure_seals_deletion_failed_without_exposing_object_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(agents_api, "get_file_storage", lambda: _FailingStorage())
    response = _client(repository).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={"scope": "tenant", "idempotency_key": "delete-tenant-0001"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "AGENT_STORAGE_CLEANUP_FAILED"
    assert repository.finished is False
    assert "opaque-object" not in response.text


def test_storage_delete_ack_without_absence_readback_never_runs_runtime_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(
        agents_api,
        "get_file_storage",
        lambda: _ReadbackPresentStorage(repository.events),
    )
    response = _client(repository).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={"scope": "tenant", "idempotency_key": "delete-tenant-readback"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["retryable"] is True
    assert response.json()["error_code"] == "AGENT_STORAGE_CLEANUP_FAILED"
    assert repository.finished is False
    assert repository.events == [
        "execution_claimed",
        "fence_verified",
        "storage_delete",
        "fence_verified",
        "storage_readback_present",
        "fence_verified",
    ]
    assert "runtime_delete" not in repository.events
    assert "opaque-object" not in response.text


@pytest.mark.parametrize("failure_at", ["delete", "readback"])
def test_storage_delete_or_readback_exception_is_retryable_without_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    failure_at: str,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(
        agents_api,
        "get_file_storage",
        lambda: _ExceptionalStorage(repository.events, failure_at),
    )
    response = _client(repository).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={
            "scope": "tenant",
            "idempotency_key": f"delete-tenant-{failure_at}-exception",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["retryable"] is True
    assert response.json()["error_code"] == "AGENT_STORAGE_CLEANUP_FAILED"
    assert repository.finished is False
    assert "runtime_delete" not in repository.events
    assert "opaque-object" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage_key",
    [
        "tenant-a/user-a/private/opaque-object",
        "tenant-a/user-a/private/lone-surrogate-\ud800",
    ],
)
async def test_governance_storage_delete_logs_use_hashes_and_exception_types_only(
    caplog: pytest.LogCaptureFixture,
    storage_key: str,
) -> None:
    file_storage = object.__new__(FileStorageService)
    file_storage._backend = _DeleteBackendSuccess()
    s3 = S3StorageBackend(
        bucket="private-bucket",
        region="test",
        access_key="",
        secret_key="",
        key_prefix="governance",
    )
    s3._client = _DeleteClientFailure()
    oss = OSSStorageBackend(
        bucket="private-bucket",
        endpoint="test",
        access_key="",
        secret_key="",
        key_prefix="governance",
    )
    oss._bucket = _DeleteBucketFailure()

    with caplog.at_level(logging.INFO):
        assert await file_storage.delete_file(storage_key) is True
        assert await s3.delete(storage_key) is False
        assert await oss.delete(storage_key) is False
        with pytest.raises(RuntimeError, match="raw-readback-secret"):
            await s3.exists(storage_key)

    success_hash = hashlib.sha256(storage_key.encode("utf-8", errors="replace")).hexdigest()
    provider_hash = hashlib.sha256(
        f"governance/{storage_key}".encode("utf-8", errors="replace")
    ).hexdigest()
    assert success_hash in caplog.text
    assert caplog.text.count(provider_hash) == 2
    assert "exception_type=RuntimeError" in caplog.text
    assert storage_key not in caplog.text
    assert "raw-provider-secret" not in caplog.text
    assert "raw-readback-secret" not in caplog.text


@pytest.mark.parametrize("unavailable", [False, True])
def test_runtime_partial_or_timeout_never_commits_sql_only_completion(
    monkeypatch: pytest.MonkeyPatch,
    unavailable: bool,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(agents_api, "get_file_storage", lambda: _SuccessfulStorage())
    response = _client(
        repository,
        _CleanupClient(partial=not unavailable, unavailable=unavailable),
    ).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={"scope": "tenant", "idempotency_key": "delete-tenant-0002"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["retryable"] is True
    assert response.json()["error_code"] == "AGENT_RUNTIME_CLEANUP_FAILED"
    assert repository.finished is False


def test_runtime_completed_receipt_allows_repository_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    monkeypatch.setattr(
        agents_api,
        "get_file_storage",
        lambda: _SuccessfulStorage(repository.events),
    )
    response = _client(repository).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions",
        json={"scope": "tenant", "idempotency_key": "delete-tenant-0003"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["retryable"] is False
    assert repository.finished is True
    assert repository.events == [
        "execution_claimed",
        "fence_verified",
        "storage_delete",
        "fence_verified",
        "storage_readback",
        "fence_verified",
        "fence_verified",
        "runtime_delete",
        "fence_verified",
    ]
    assert "test-claim-token" not in response.text
    assert "_execution_" not in response.text


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        ({"scope": "user", "idempotency_key": "delete-user-0001"}, "subject_user_id"),
        (
            {
                "scope": "tenant",
                "subject_user_id": "user-a",
                "idempotency_key": "delete-tenant-0001",
            },
            "subject_user_id",
        ),
    ],
)
def test_deletion_scope_subject_shape_is_closed(
    payload: dict[str, Any], expected_fragment: str
) -> None:
    response = _client(_Repository()).post(
        f"/api/v1/agents/{AGENT_ID}/governance/data-deletions", json=payload
    )
    assert response.status_code == 422
    assert expected_fragment in response.text
