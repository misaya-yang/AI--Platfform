from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.persistence.repositories.agent_repository import validate_agent_spec
from ai_gateway_core.persistence.repositories.agent_resource_resolver import (
    AgentKnowledgeAuthorizationError,
    DatabaseAgentKnowledgeResolver,
)
from assistant_service.core.agent.agent_loop import AgentLoop
from assistant_service.core.assistant_service import AssistantService
from assistant_service.core.tools.builtin_tools import KBSearchExecutor
from assistant_service.core.tools.tool_registry import ToolCallRequest
from fastapi import HTTPException, Request
from knowledge_service.services.knowledge.dataset_service import (
    _dataset_revision_fingerprint,
)

from src.api.v1.agent_runtime import _build_snapshot
from src.core.auth.user_resolver import UserContext


class _Acquire:
    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection: Any):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _Connection:
    def __init__(self, allowed: list[str]):
        self.allowed = allowed
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any):
        self.calls.append((query, args))
        return [{"dataset_id": value} for value in self.allowed]


@pytest.mark.asyncio
async def test_runtime_knowledge_resolver_requires_complete_acl_result() -> None:
    connection = _Connection(["dataset-a"])
    resolver = DatabaseAgentKnowledgeResolver(
        SimpleNamespace(enabled=True, _pool=_Pool(connection))
    )
    bindings = [
        {"dataset_id": "dataset-a", "retrieval_config": {"top_k": 4}},
        {"dataset_id": "dataset-b", "retrieval_config": {"top_k": 6}},
    ]

    with pytest.raises(AgentKnowledgeAuthorizationError):
        await resolver.resolve(
            tenant_id="tenant-a",
            user_id="user-a",
            bindings=bindings,
        )
    query, args = connection.calls[0]
    assert "dataset.tenant_id = $1" in query
    assert "dataset.is_deleted = FALSE" in query
    assert "dataset_permissions" in query
    assert args[0:3] == ("tenant-a", "user-a", ["dataset-a", "dataset-b"])


def _resolution() -> dict[str, Any]:
    return {
        "agent": {
            "tenant_id": "tenant-a",
            "agent_id": "11111111-1111-4111-8111-111111111111",
        },
        "draft": {},
        "version": {
            "agent_version_id": "22222222-2222-4222-8222-222222222222",
            "spec_hash": "sha256:spec",
        },
        "publication": {
            "publication_id": "33333333-3333-4333-8333-333333333333",
            "channel": "api",
            "auth_mode": "tenant",
            "policy": {},
        },
        "spec": {
            "model": {"model_id": "qwen3.7-plus"},
            "instructions": "Use the bound live Dataset.",
            "memory": {"mode": "session"},
        },
        "capabilities": [],
        "knowledge": [
            {
                "dataset_id": "dataset-a",
                "retrieval_config": {
                    "mode": "tool",
                    "top_k": 4,
                    "threshold": 0.5,
                },
            }
        ],
    }


def _request(knowledge_resolver: Any) -> Request:
    class ModelResolver:
        def resolve(self, **_kwargs):
            return {"id": "qwen3.7-plus", "provider": "dashscope"}

    class CapabilityResolver:
        def resolve(self, **kwargs):
            return kwargs["bindings"]

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/agent-runtime/test/chat/stream",
            "query_string": b"",
            "headers": [],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    agent_runtime_model_resolver=ModelResolver(),
                    agent_runtime_capability_resolver=CapabilityResolver(),
                    agent_runtime_knowledge_resolver=knowledge_resolver,
                )
            ),
        }
    )


@pytest.mark.asyncio
async def test_gateway_run_fails_closed_after_dataset_revoke_or_delete() -> None:
    class Revoked:
        def resolve(self, **_kwargs):
            return []

    with pytest.raises(HTTPException) as error:
        await _build_snapshot(
            _request(Revoked()),
            _resolution(),
            UserContext(
                tenant_id="tenant-a",
                user_id="user-a",
                is_authenticated=True,
            ),
            channel="api",
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "AGENT_KNOWLEDGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_snapshot_marks_live_content_as_provenance_only_not_replayable() -> None:
    class Authorized:
        def resolve(self, **kwargs):
            return kwargs["bindings"]

    snapshot = await _build_snapshot(
        _request(Authorized()),
        _resolution(),
        UserContext(
            tenant_id="tenant-a",
            user_id="user-a",
            is_authenticated=True,
        ),
        channel="api",
    )

    retrieval = snapshot["knowledge"]["retrieval"]
    assert retrieval["provenance"] == [
        {
            "dataset_id": "dataset-a",
            "content_mode": "live_latest",
            "historical_replayable": False,
            "revision_source": "assistant_run_catalog",
        }
    ]
    assert retrieval["replayability"] == "live_content_provenance_only"
    assert snapshot["fingerprints"]["knowledge_revision"].startswith("sha256:")


def test_agent_knowledge_config_rejects_unknown_fields_and_invalid_bindings() -> None:
    spec = {
        "schema_version": "agent-spec/v1",
        "identity": {},
        "instructions": "Use the exact bound Datasets.",
        "model": {"model_id": "qwen3.7-plus"},
        "capabilities": [],
        "knowledge": [
            {
                "dataset_id": "dataset-a",
                "retrieval_config": {"mode": "tool", "top_k": 4},
            },
            {
                "dataset_id": "dataset-b",
                "retrieval_config": {"mode": "auto", "unsupported": True},
            },
        ],
        "memory": {},
    }

    errors = validate_agent_spec(spec)

    assert {error["code"] for error in errors} >= {
        "AGENT_SPEC_FIELD_FORBIDDEN",
    }
    assert "AGENT_KNOWLEDGE_MODE_MISMATCH" not in {error["code"] for error in errors}

    identity_errors = validate_agent_spec(
        {
            **spec,
            "knowledge": [
                {"dataset_id": "dataset-a", "retrieval_config": {}},
                {"dataset_id": "dataset-a", "retrieval_config": {}},
                {"retrieval_config": {}},
            ],
        }
    )
    assert {error["code"] for error in identity_errors} >= {
        "AGENT_KNOWLEDGE_DATASET_DUPLICATE",
        "AGENT_KNOWLEDGE_DATASET_REQUIRED",
    }


@pytest.mark.asyncio
async def test_snapshot_preserves_each_dataset_retrieval_and_image_config() -> None:
    class Authorized:
        def resolve(self, **kwargs):
            return kwargs["bindings"]

    resolution = _resolution()
    resolution["knowledge"] = [
        {
            "dataset_id": "dataset-b",
            "retrieval_config": {
                "mode": "tool",
                "top_k": 17,
                "threshold": 0.85,
                "include_images": True,
            },
        },
        {
            "dataset_id": "dataset-a",
            "retrieval_config": {
                "mode": "auto",
                "top_k": 4,
                "threshold": 0.2,
                "include_images": False,
            },
        },
    ]

    snapshot = await _build_snapshot(
        _request(Authorized()),
        resolution,
        UserContext(
            tenant_id="tenant-a",
            user_id="user-a",
            is_authenticated=True,
        ),
        channel="api",
    )

    retrieval = snapshot["knowledge"]["retrieval"]
    assert snapshot["knowledge"]["datasets"] == ["dataset-a", "dataset-b"]
    assert retrieval["config_scope"] == "per_dataset"
    assert retrieval["mode"] == "auto"
    assert retrieval["top_k"] == 17
    assert retrieval["threshold"] == 0.2
    assert retrieval["include_images"] is True
    assert retrieval["by_dataset"] == {
        "dataset-a": {
            "mode": "auto",
            "top_k": 4,
            "threshold": 0.2,
            "include_images": False,
        },
        "dataset-b": {
            "mode": "tool",
            "top_k": 17,
            "threshold": 0.85,
            "include_images": True,
        },
    }


class _KnowledgeService:
    def __init__(self) -> None:
        self.updated_at = "2026-07-18T00:00:00Z"
        self.revision_fingerprint = "sha256:" + "1" * 64
        self.include = True
        self.document_count = 2
        self.segment_count = 8

    async def list_datasets(self, _user):
        if not self.include:
            return []
        return [
            {
                "dataset_id": "dataset-a",
                "name": "Dataset A",
                "updated_at": self.updated_at,
                "revision_fingerprint": self.revision_fingerprint,
                "embedding_provider": "dashscope",
                "embedding_model": "text-embedding-v3",
                "embedding_dimension": 1024,
                "needs_reindex": False,
                "collection_name": "dataset-a",
                "statistics": {
                    "document_count": self.document_count,
                    "segment_count": self.segment_count,
                },
            },
            {
                "dataset_id": "dataset-unbound",
                "name": "Must Not Leak Into Agent Context",
                "updated_at": self.updated_at,
                "revision_fingerprint": "sha256:" + "f" * 64,
                "statistics": {"document_count": 1, "segment_count": 1},
            },
        ]


@pytest.mark.asyncio
async def test_each_run_captures_live_revision_and_explicit_replay_limit() -> None:
    service = _KnowledgeService()
    loop = object.__new__(AgentLoop)
    loop.kb_service = service
    context = SimpleNamespace(
        config=SimpleNamespace(kb_dataset_ids=["dataset-a"]),
        knowledge_provenance={},
    )

    names, first_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )
    first = dict(context.knowledge_provenance)
    # Same Dataset metadata and counts, but a supported content edit advances
    # the authoritative Knowledge content revision.
    service.revision_fingerprint = "sha256:" + "2" * 64
    _names, second_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )

    assert names == {"dataset-a": "Dataset A"}
    assert first_hash != second_hash
    assert first == {
        "state": "available",
        "dataset_ids": ["dataset-a"],
        "revision_hash": first_hash,
        "content_mode": "live_latest",
        "historical_replayable": False,
        "catalog_complete": True,
    }


@pytest.mark.asyncio
async def test_config_only_catalog_change_changes_next_run_provenance_hash() -> None:
    service = _KnowledgeService()
    loop = object.__new__(AgentLoop)
    loop.kb_service = service
    context = SimpleNamespace(
        config=SimpleNamespace(
            kb_dataset_ids=["dataset-a"],
            kb_retrieval_configs={
                "dataset-a": {
                    "mode": "tool",
                    "top_k": 4,
                    "threshold": 0.2,
                    "include_images": False,
                }
            },
        ),
        knowledge_provenance={},
    )
    dataset = {
        "dataset_id": "dataset-a",
        "content_revision": 7,
        "embedding_provider": "dashscope",
        "embedding_model": "text-embedding-v3",
        "embedding_dimension": 1024,
        "needs_reindex": False,
        "collection_name": "dataset-a",
        "index_config": {"retrieval": {"mode": "hybrid", "score_threshold": 0.2}},
    }
    service.revision_fingerprint = str(_dataset_revision_fingerprint(dataset))
    _names, first_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )
    dataset["index_config"] = {"retrieval": {"mode": "bm25", "score_threshold": 0.9}}
    service.revision_fingerprint = str(_dataset_revision_fingerprint(dataset))
    _names, second_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )

    assert first_hash != second_hash


@pytest.mark.asyncio
async def test_catalog_telemetry_and_counts_do_not_change_run_hash() -> None:
    service = _KnowledgeService()
    loop = object.__new__(AgentLoop)
    loop.kb_service = service
    context = SimpleNamespace(
        config=SimpleNamespace(kb_dataset_ids=["dataset-a"]),
        knowledge_provenance={},
    )

    _names, first_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )
    service.updated_at = "2026-07-19T23:59:59Z"
    service.document_count = 200
    service.segment_count = 800
    _names, second_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )

    assert first_hash == second_hash


@pytest.mark.asyncio
async def test_kb_executor_applies_sealed_config_per_dataset() -> None:
    class RecordingKnowledge:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def retrieve_with_images_v2(self, **kwargs):
            self.calls.append(dict(kwargs))
            return [], {"dataset_name": kwargs["dataset_id"]}

    knowledge = RecordingKnowledge()
    executor = KBSearchExecutor(knowledge)
    result = await executor.execute(
        ToolCallRequest(
            call_id="kb-call",
            tool_name="search_knowledge_base",
            arguments={
                "query": "exact config",
                "dataset_ids": ["dataset-a", "dataset-b"],
                "top_k": 1,
                "score_threshold": 0.99,
            },
            user=SimpleNamespace(user_id="user-a", tenant_id="tenant-a"),
            metadata={
                "kb_retrieval_configs": {
                    "dataset-a": {
                        "mode": "tool",
                        "top_k": 4,
                        "threshold": 0.2,
                        "include_images": False,
                    },
                    "dataset-b": {
                        "mode": "tool",
                        "top_k": 17,
                        "threshold": 0.85,
                        "include_images": True,
                    },
                }
            },
        )
    )

    assert result.success is True
    calls = {call["dataset_id"]: call for call in knowledge.calls}
    assert calls["dataset-a"]["top_k"] == 4
    assert calls["dataset-a"]["score_threshold"] == 0.2
    assert calls["dataset-a"]["include_images"] is False
    assert calls["dataset-b"]["top_k"] == 17
    assert calls["dataset-b"]["score_threshold"] == 0.85
    assert calls["dataset-b"]["include_images"] is True


@pytest.mark.asyncio
async def test_auto_rag_applies_sealed_config_per_dataset() -> None:
    class RecordingKnowledge:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        @staticmethod
        def _result(dataset_id: str):
            return [
                SimpleNamespace(
                    metadata={},
                    image_url=None,
                    text=f"context-{dataset_id}",
                    score=0.8,
                    segment_id=f"segment-{dataset_id}",
                    document_id=f"document-{dataset_id}",
                )
            ], {"dataset_name": dataset_id}

        async def retrieve(self, **kwargs):
            self.calls.append({"method": "text", **kwargs})
            return self._result(kwargs["dataset_id"])

        async def retrieve_with_images(self, **kwargs):
            self.calls.append({"method": "images", **kwargs})
            return self._result(kwargs["dataset_id"])

    knowledge = RecordingKnowledge()
    service = object.__new__(AssistantService)
    service.kb_service = knowledge

    contexts = await service._retrieve_context(  # noqa: SLF001
        user=SimpleNamespace(user_id="user-a", tenant_id="tenant-a"),
        query="exact auto config",
        dataset_ids=["dataset-a", "dataset-b"],
        top_k=1,
        score_threshold=0.99,
        include_images=False,
        retrieval_configs={
            "dataset-a": {
                "mode": "auto",
                "top_k": 4,
                "threshold": 0.2,
                "include_images": False,
            },
            "dataset-b": {
                "mode": "auto",
                "top_k": 17,
                "threshold": 0.85,
                "include_images": True,
            },
        },
    )

    assert len(contexts) == 2
    calls = {call["dataset_id"]: call for call in knowledge.calls}
    assert calls["dataset-a"]["method"] == "text"
    assert calls["dataset-a"]["top_k"] == 4
    assert calls["dataset-a"]["score_threshold"] == 0.2
    assert calls["dataset-b"]["method"] == "images"
    assert calls["dataset-b"]["top_k"] == 17
    assert calls["dataset-b"]["score_threshold"] == 0.85


@pytest.mark.asyncio
async def test_bound_dataset_without_authoritative_revision_is_unavailable() -> None:
    service = _KnowledgeService()
    service.revision_fingerprint = ""
    loop = object.__new__(AgentLoop)
    loop.kb_service = service
    context = SimpleNamespace(
        config=SimpleNamespace(kb_dataset_ids=["dataset-a"]),
        knowledge_provenance={},
    )

    names, revision_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )

    assert names == {"dataset-a": "Dataset A"}
    assert context.knowledge_provenance == {
        "state": "unavailable",
        "dataset_ids": ["dataset-a"],
        "revision_hash": revision_hash,
        "content_mode": "live_latest",
        "historical_replayable": False,
        "catalog_complete": False,
    }


@pytest.mark.asyncio
async def test_missing_live_dataset_produces_unavailable_provenance() -> None:
    service = _KnowledgeService()
    service.include = False
    loop = object.__new__(AgentLoop)
    loop.kb_service = service
    context = SimpleNamespace(
        config=SimpleNamespace(kb_dataset_ids=["dataset-a"]),
        knowledge_provenance={},
    )

    names, revision_hash = await loop._get_streaming_dataset_context(  # noqa: SLF001
        context,
        SimpleNamespace(),
    )
    assert names is None
    assert context.knowledge_provenance == {
        "state": "unavailable",
        "dataset_ids": ["dataset-a"],
        "revision_hash": revision_hash,
        "content_mode": "live_latest",
        "historical_replayable": False,
        "catalog_complete": False,
    }
