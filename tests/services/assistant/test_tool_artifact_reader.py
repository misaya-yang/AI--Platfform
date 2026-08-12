from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.storage.artifact_storage import ArtifactStorageService
from assistant_service.core.agent.subagent_manager import SubAgentManager
from assistant_service.core.agent.subagent_types import (
    SUBAGENT_DEFAULTS,
    SubAgentConfig,
    SubAgentType,
)
from assistant_service.core.tool_invoker import (
    CapabilityAllowlist,
    RegistryToolInvoker,
    ToolInvocationContext,
)
from assistant_service.core.tools.tool_artifact_reader import (
    READ_TOOL_ARTIFACT_DEFINITION,
    ReadToolArtifactExecutor,
    register_tool_artifact_reader,
)
from assistant_service.core.tools.tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolRegistry,
    ToolRiskLevel,
)


def _artifact(content: bytes, **overrides: Any) -> Any:
    metadata = {
        "schema_version": "assistant-tool-output-artifact/v1",
        "redacted": True,
        "complete_redacted": True,
        "content_kind": "text",
        "content_chars": len(content.decode("utf-8")),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "host_receipt_id": "spill_hostreceipt1",
    }
    metadata.update(overrides.pop("metadata", {}))
    turn_id = overrides.pop("turn_id", "spill_hostreceipt1")
    return SimpleNamespace(
        artifact_id="art_12345678",
        source="tool_output_spill",
        turn_id=turn_id,
        metadata=metadata,
        **overrides,
    )


class _Storage:
    def __init__(self, content: bytes, *, artifact: Any | None = None) -> None:
        self.content = content
        self.artifact = artifact or _artifact(content)
        self.expected_scope = {
            "tenant_id": "tenant-a",
            "session_id": "session-a",
            "user_id": "user-a",
        }
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def read_artifact_scoped(self, artifact_id: str, *, max_bytes: int, **scope: str):
        self.calls.append((artifact_id, scope))
        assert max_bytes == 2_000_000
        if artifact_id != self.artifact.artifact_id or scope != self.expected_scope:
            return None
        return self.artifact, self.content

    def supports_scoped_artifact_reads(self) -> bool:
        return True


def _request(arguments: dict[str, Any], **scope: str) -> ToolCallRequest:
    return ToolCallRequest(
        call_id="call-1",
        tool_name="read_tool_artifact",
        arguments=arguments,
        metadata={
            "tenant_id": scope.get("tenant_id", "tenant-a"),
            "session_id": scope.get("session_id", "session-a"),
            "user_id": scope.get("user_id", "user-a"),
        },
    )


@pytest.mark.asyncio
async def test_reads_bounded_slice_with_verified_completeness_receipt() -> None:
    content = b"0123456789TAIL-AUTHORITY"
    storage = _Storage(content)
    executor = ReadToolArtifactExecutor(storage)

    result = await executor.execute(
        _request({"artifact_id": "art_12345678", "offset": 10, "limit": 20})
    )

    assert result.success is True
    assert result.result["content"] == "TAIL-AUTHORITY"
    assert result.result["next_offset"] is None
    assert result.result["complete"] is True
    assert result.result["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert result.result["redaction_receipt"]["host_verified"] is True
    assert result.metadata["artifact_read_verified"] is True


@pytest.mark.asyncio
async def test_cjk_slice_honors_token_budget_and_returns_next_offset() -> None:
    content = ("证券法证据" * 1_000).encode("utf-8")
    executor = ReadToolArtifactExecutor(_Storage(content))

    result = await executor.execute(
        _request({"artifact_id": "art_12345678", "offset": 0, "limit": 256})
    )

    assert result.success is True
    assert result.result["returned_tokens_estimate"] <= 256
    assert result.result["next_offset"] == len(result.result["content"])
    assert result.result["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ({"tenant_id": "tenant-b"}, "ARTIFACT_NOT_FOUND"),
        ({"session_id": "session-b"}, "ARTIFACT_NOT_FOUND"),
        ({"user_id": "user-b"}, "ARTIFACT_NOT_FOUND"),
    ],
)
async def test_cross_scope_reads_are_not_disclosed(scope: dict[str, str], expected: str) -> None:
    executor = ReadToolArtifactExecutor(_Storage(b"private"))

    result = await executor.execute(_request({"artifact_id": "art_12345678"}, **scope))

    assert result.success is False
    assert result.error == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"artifact_id": "../../secret"}, "ARTIFACT_READ_INVALID"),
        ({"artifact_id": "https://example/art_12345678"}, "ARTIFACT_READ_INVALID"),
        ({"artifact_id": "art_missing99"}, "ARTIFACT_NOT_FOUND"),
        ({"artifact_id": "art_12345678", "limit": 20_001}, "ARTIFACT_READ_INVALID"),
        ({"artifact_id": "art_12345678", "offset": 999}, "ARTIFACT_OFFSET_OUT_OF_RANGE"),
    ],
)
async def test_rejects_ids_urls_and_out_of_range_reads(
    arguments: dict[str, Any], expected: str
) -> None:
    executor = ReadToolArtifactExecutor(_Storage(b"short"))

    result = await executor.execute(_request(arguments))

    assert result.success is False
    assert result.error == expected


@pytest.mark.asyncio
async def test_rejects_forged_or_tampered_storage_receipt() -> None:
    content = b"complete evidence"
    forged = _artifact(
        content,
        metadata={
            "schema_version": "assistant-tool-output-artifact/v1",
            "redacted": True,
            "complete_redacted": True,
            "content_kind": "text",
            "content_chars": len(content.decode()),
            "content_sha256": "0" * 64,
            "host_receipt_id": "spill_hostreceipt1",
        },
    )
    executor = ReadToolArtifactExecutor(_Storage(content, artifact=forged))

    result = await executor.execute(_request({"artifact_id": "art_12345678"}))

    assert result.success is False
    assert result.error == "ARTIFACT_INTEGRITY_FAILED"


@pytest.mark.asyncio
async def test_rejects_forged_host_receipt_marker() -> None:
    content = b"complete evidence"
    forged = _artifact(content, turn_id="attacker-controlled")
    executor = ReadToolArtifactExecutor(_Storage(content, artifact=forged))

    result = await executor.execute(_request({"artifact_id": "art_12345678"}))

    assert result.success is False
    assert result.error == "ARTIFACT_INTEGRITY_FAILED"


def test_reader_schema_has_no_path_or_url_and_hard_bounds() -> None:
    schema = READ_TOOL_ARTIFACT_DEFINITION.json_argument_schema()
    assert set(schema["properties"]) == {"artifact_id", "offset", "limit"}
    assert schema["properties"]["limit"]["maximum"] <= 20_000
    assert schema["properties"]["offset"]["minimum"] == 0


def test_reader_registration_fails_closed_without_scope_revalidation() -> None:
    storage = _Storage(b"private")
    storage.supports_scoped_artifact_reads = lambda: False

    assert register_tool_artifact_reader(storage) is False


def test_reader_registration_fails_closed_when_scope_gate_raises() -> None:
    storage = _Storage(b"private")

    def broken_scope_gate() -> bool:
        raise RuntimeError("database unavailable")

    storage.supports_scoped_artifact_reads = broken_scope_gate

    assert register_tool_artifact_reader(storage) is False


@pytest.mark.asyncio
async def test_subagent_reader_visibility_is_parent_capability_intersection() -> None:
    registry = ToolRegistry()

    async def source(request: ToolCallRequest) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result="evidence",
        )

    registry.register(
        ToolDefinition(
            name="read_source",
            description="Read source evidence",
            parameters=[],
            category=ToolCategory.RETRIEVAL,
            risk_level=ToolRiskLevel.LOW,
            capability_metadata={"operation_kind": "read"},
        ),
        source,
    )
    registry.register(READ_TOOL_ARTIFACT_DEFINITION, ReadToolArtifactExecutor(_Storage(b"safe")))
    manager = SubAgentManager(
        model_registry=SimpleNamespace(_models={}),
        tool_registry=registry,
        tool_invoker=RegistryToolInvoker(tool_registry=registry),
    )

    async def visible(parent_names: set[str]) -> set[str]:
        parent = ToolInvocationContext(
            session_id="session-a",
            user_id="user-a",
            tenant_id="tenant-a",
            request_id="request-a",
            user=SimpleNamespace(user_id="user-a", is_authenticated=True, roles=[]),
            capability_allowlist=CapabilityAllowlist(frozenset(parent_names)),
        )
        tools, _ = await manager._get_tools(
            SubAgentConfig(agent_type=SubAgentType.TASK, prompt="read evidence"),
            SUBAGENT_DEFAULTS[SubAgentType.TASK],
            parent.user,
            agent_id="child-a",
            parent_tenant_id=parent.tenant_id,
            parent_invocation_context=parent,
            kb_dataset_ids=None,
        )
        return {tool.name for tool in tools}

    assert await visible({"read_source"}) == {"read_source"}
    assert await visible({"read_source", "read_tool_artifact"}) == {
        "read_source",
        "read_tool_artifact",
    }


class _Connection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.row


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Backend:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.downloaded: list[str] = []

    async def download(self, storage_key: str) -> bytes:
        self.downloaded.append(storage_key)
        return self.content


@pytest.mark.asyncio
async def test_storage_rechecks_all_scopes_in_one_query_before_download() -> None:
    row = {"storage_key": "host/verified/key", "size_bytes": 12}
    connection = _Connection(row)
    backend = _Backend(b"safe content")
    service = ArtifactStorageService.__new__(ArtifactStorageService)
    service.database = SimpleNamespace(_pool=_Pool(connection))
    service._backend = backend
    service._row_to_artifact = lambda value: SimpleNamespace(**value)

    result = await service.read_artifact_scoped(
        "art_12345678",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="user-a",
        max_bytes=2_000_000,
    )

    assert result is not None
    query, args = connection.calls[0]
    assert all(
        field in query for field in ("tenant_id", "session_id", "user_id", "source", "turn_id")
    )
    assert args == ("art_12345678", "tenant-a", "session-a", "user-a")
    assert backend.downloaded == ["host/verified/key"]


@pytest.mark.asyncio
async def test_storage_rejects_oversized_metadata_before_backend_download() -> None:
    connection = _Connection({"storage_key": "too/large", "size_bytes": 2_000_001})
    backend = _Backend(b"")
    service = ArtifactStorageService.__new__(ArtifactStorageService)
    service.database = SimpleNamespace(_pool=_Pool(connection))
    service._backend = backend
    service._row_to_artifact = lambda value: SimpleNamespace(**value)

    result = await service.read_artifact_scoped(
        "art_12345678",
        tenant_id="tenant-a",
        session_id="session-a",
        user_id="user-a",
        max_bytes=2_000_000,
    )

    assert result is None
    assert backend.downloaded == []
