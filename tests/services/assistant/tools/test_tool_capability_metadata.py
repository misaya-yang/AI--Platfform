"""Repository-owned tool side-effect metadata and dispatch contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ai_gateway_core.skills import SkillManifest
from assistant_service.core.skills.tool_bridge import SkillToolBridge
from assistant_service.core.tool_invoker import RegistryToolInvoker, ToolInvocationContext
from assistant_service.core.tools.confluence_tool import CONFLUENCE_READ_DEFINITION
from assistant_service.core.tools.context_tools import (
    CONTEXT_COMPACT_DEFINITION,
    ContextCompactExecutor,
)
from assistant_service.core.tools.document_generator_tool import (
    DOCUMENT_GENERATION_DEFINITION,
    _definition_for_executor,
)
from assistant_service.core.tools.pptx_generator_tool import PPTX_GENERATION_DEFINITION
from assistant_service.core.tools.primitives import (
    FS_GLOB_DEFINITION,
    FS_GREP_DEFINITION,
    FS_READ_DEFINITION,
)
from assistant_service.core.tools.quiz_tool import QUIZ_GENERATION_DEFINITION
from assistant_service.core.tools.subagent_tool import SPAWN_SUBAGENT_DEFINITION
from assistant_service.core.tools.todo_tools import TODO_READ_DEFINITION, TODO_WRITE_DEFINITION
from assistant_service.core.tools.tool_registry import ToolDefinition, ToolRegistry
from assistant_service.core.tools.web_fetch import (
    WEB_FETCH_DEFINITION,
    SSRFError,
    WebFetchExecutor,
)


@pytest.mark.parametrize(
    ("definition", "external_service"),
    [
        (CONFLUENCE_READ_DEFINITION, True),
        (FS_READ_DEFINITION, False),
        (FS_GLOB_DEFINITION, False),
        (FS_GREP_DEFINITION, False),
        (TODO_READ_DEFINITION, False),
        (WEB_FETCH_DEFINITION, True),
    ],
)
def test_repository_read_tools_declare_replay_safe_metadata(
    definition: ToolDefinition,
    external_service: bool,
) -> None:
    metadata = definition.capability_metadata

    assert metadata["operation_kind"] == "read"
    assert metadata["read_only"] is True
    assert bool(metadata.get("external_service")) is external_service


@pytest.mark.parametrize(
    "definition",
    [
        CONTEXT_COMPACT_DEFINITION,
        DOCUMENT_GENERATION_DEFINITION,
        PPTX_GENERATION_DEFINITION,
        QUIZ_GENERATION_DEFINITION,
        TODO_WRITE_DEFINITION,
    ],
)
def test_repository_control_and_artifact_tools_declare_write_metadata(
    definition: ToolDefinition,
) -> None:
    assert definition.capability_metadata["operation_kind"] == "write"


def test_external_write_boundaries_are_declared() -> None:
    assert QUIZ_GENERATION_DEFINITION.capability_metadata["external_service"] is True


def test_subagent_dispatch_is_non_mutating_and_never_blindly_retried() -> None:
    metadata = SPAWN_SUBAGENT_DEFINITION.capability_metadata

    assert metadata["operation_kind"] == "read"
    assert metadata["read_only"] is True
    assert metadata["external_service"] is True
    assert SPAWN_SUBAGENT_DEFINITION.max_retries == 0


def test_document_tool_advertises_only_runtime_supported_formats() -> None:
    unavailable_pdf = SimpleNamespace(pdf_converter=SimpleNamespace(is_available=False))
    definition = _definition_for_executor(unavailable_pdf)  # type: ignore[arg-type]
    format_parameter = next(item for item in definition.parameters if item.name == "format")

    assert format_parameter.enum == ["docx", "md"]
    original_format = next(
        item for item in DOCUMENT_GENERATION_DEFINITION.parameters if item.name == "format"
    )
    assert original_format.enum == ["docx", "pdf", "md"]


class _ToolRegistry:
    def __init__(self) -> None:
        self.definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition, _executor: Any) -> None:
        self.definitions[definition.name] = definition


@pytest.mark.parametrize(
    ("name", "entrypoint", "permissions", "expected_kind", "expected_read_only"),
    [
        ("reader", "md://reader", ["kb:read", "web:search"], "read", True),
        ("tenant_writer", "db://tenant-writer", ["skill:create"], "read", True),
        ("builtin", "builtin://builtin", ["kb:read"], "unknown", False),
    ],
)
def test_dynamic_skill_tools_classify_the_actual_bridge_boundary(
    name: str,
    entrypoint: str,
    permissions: list[str],
    expected_kind: str,
    expected_read_only: bool,
) -> None:
    registry = _ToolRegistry()
    bridge = SkillToolBridge(skill_registry=object(), tool_registry=registry)
    bridge.register_skill_as_tool(
        SkillManifest(
            name=name,
            title=name,
            description=f"{name} test skill",
            entrypoint=entrypoint,
            permissions=permissions,
        )
    )

    metadata = registry.definitions[f"skill_{name}"].capability_metadata
    assert metadata["operation_kind"] == expected_kind
    assert bool(metadata.get("read_only")) is expected_read_only


def _context() -> ToolInvocationContext:
    return ToolInvocationContext(
        session_id="side-effect-catalog-session",
        user_id="side-effect-catalog-user",
        tenant_id="side-effect-catalog-tenant",
        request_id="side-effect-catalog-request",
    )


@pytest.mark.asyncio
async def test_context_compact_validation_remains_pre_effect_failure() -> None:
    registry = ToolRegistry()
    registry.register(CONTEXT_COMPACT_DEFINITION, ContextCompactExecutor())

    result = await RegistryToolInvoker(registry).invoke(
        "context_compact",
        {"keep_recent_turns": "lots"},
        _context(),
    )

    assert result.success is False
    assert result.error == "TOOL_ARGUMENT_VALIDATION_FAILED"
    validation = result.metadata["tool_argument_validation"]
    assert validation["valid"] is False
    assert validation["issues"] == [
        {"path": "$.keep_recent_turns", "rule": "type", "expected": "integer"}
    ]
    assert result.metadata.get("side_effect_unknown") is not True


@pytest.mark.asyncio
async def test_web_fetch_ssrf_rejection_remains_original_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_fetch(**_kwargs: Any) -> dict[str, Any]:
        raise SSRFError("private address blocked")

    monkeypatch.setattr(
        "assistant_service.core.tools.web_fetch.web_fetch",
        reject_fetch,
    )
    registry = ToolRegistry()
    registry.register(WEB_FETCH_DEFINITION, WebFetchExecutor())

    result = await RegistryToolInvoker(registry).invoke(
        "web_fetch",
        {"url": "http://127.0.0.1/private"},
        _context(),
    )

    assert result.success is False
    assert result.error == "URL rejected: private address blocked"
    assert result.metadata.get("side_effect_unknown") is not True
