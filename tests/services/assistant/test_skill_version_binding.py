from __future__ import annotations

from types import SimpleNamespace

import pytest
from ai_gateway_core.agents import VerifiedAgentRuntime
from ai_gateway_core.skills import SkillManifest, SkillRegistry, SkillSource
from assistant_service.api.routes.chat import _build_agent_runtime_config
from assistant_service.core.skills.tool_bridge import SkillToolBridge, skill_tool_name
from assistant_service.core.tool_invoker import RegistryToolInvoker, ToolInvocationContext
from assistant_service.core.tools.tool_registry import ToolRegistry

OLD_VERSION = "21111111-1111-4111-8111-111111111111"
NEW_VERSION = "22222222-2222-4222-8222-222222222222"


def _manifest(version_id: str, instructions: str) -> SkillManifest:
    skill_id = "11111111-1111-4111-8111-111111111111"
    return SkillManifest(
        name="report-helper",
        title="Report Helper",
        description="Creates a report",
        summary="Creates a report",
        entrypoint=f"db://{skill_id}/{version_id}",
        instructions=instructions,
        permissions=["knowledge:read"],
        source=SkillSource.USER,
        artifact_type="tenant_instruction",
        skill_id=skill_id,
        version_id=version_id,
        content_hash="a" * 64,
        generated=False,
        enabled=True,
    )


def _verified() -> VerifiedAgentRuntime:
    snapshot = {
        "schema_version": "agent-runtime/v1",
        "tenant_id": "tenant-a",
        "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "agent_version_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "publication": {"id": None, "channel": "preview", "auth_mode": "private"},
        "model": {
            "id": "qwen3.7-plus",
            "provider": "dashscope",
            "parameters": {"temperature": 0.2},
        },
        "instructions": {"agent": "Use the exact Skill.", "prompt_hash": "sha256:prompt"},
        "capabilities": [
            {
                "type": "skill",
                "id": "report-helper",
                "version": OLD_VERSION,
                "schema_hash": "sha256:" + "a" * 64,
                "risk": "low",
                "config": {},
            }
        ],
        "knowledge": {"datasets": [], "retrieval": {"mode": "off"}},
        "memory": {"mode": "session"},
        "channel_policy": {
            "attachments": True,
            "high_risk_tools": False,
            "allowed_origins": [],
        },
        "fingerprints": {
            "spec": "sha256:spec",
            "tool_schema": "sha256:tools",
            "skills": "sha256:skills",
            "knowledge_revision": "sha256:knowledge",
        },
    }
    return VerifiedAgentRuntime(
        tenant_id="tenant-a",
        caller_principal="user-a",
        agent_id=snapshot["agent_id"],
        agent_version_id=snapshot["agent_version_id"],
        draft_revision=None,
        publication_id=None,
        channel="preview",
        session_id="session-a",
        runtime_fingerprint="sha256:runtime",
        spec_hash="sha256:spec",
        capability_ids=frozenset({"report-helper"}),
        resolved_snapshot=snapshot,
    )


class _Policy:
    def allowed_tool_names(self, **_kwargs):
        return {"report-helper"}

    def allowed_dataset_ids(self, **_kwargs):
        return set()


def test_signed_snapshot_preserves_exact_skill_version_to_invocation_allowlist() -> None:
    config = _build_agent_runtime_config(_verified(), tenant_policy=_Policy())

    expected_tool = skill_tool_name("report-helper", OLD_VERSION)
    assert config.allowed_skill_ids == frozenset({"report-helper"})
    assert config.allowed_skill_versions == {"report-helper": OLD_VERSION}
    assert config.capability_allowlist is not None
    assert config.capability_allowlist.tool_names == frozenset({expected_tool})


def test_agent_skill_kill_switch_preserves_binding_but_disables_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_STUDIO_SKILLS_ENABLED", "false")

    config = _build_agent_runtime_config(_verified(), tenant_policy=_Policy())

    assert config.skills_enabled is False
    assert config.allowed_skill_versions == {"report-helper": OLD_VERSION}
    assert config.capability_allowlist is not None
    assert config.capability_allowlist.tool_names == frozenset(
        {skill_tool_name("report-helper", OLD_VERSION)}
    )


@pytest.mark.asyncio
async def test_runtime_loads_only_full_exact_content_and_later_version_does_not_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _manifest(OLD_VERSION, "OLD IMMUTABLE INSTRUCTIONS")
    new = _manifest(NEW_VERSION, "NEW INSTRUCTIONS")

    class Repository:
        def __init__(self, _database):
            pass

        async def load_versions(self, **values):
            assert values["tenant_id"] == "tenant-a"
            assert values["user_id"] == "user-a"
            return [old if OLD_VERSION in values["version_ids"] else new]

    import ai_gateway_core.skills.registry as registry_module

    monkeypatch.setattr(registry_module, "DatabaseSkillArtifactRepository", Repository)
    registry = SkillRegistry(database=object())
    loaded = await registry.load_versions_from_database(
        "tenant-a",
        "user-a",
        allowed_versions={"report-helper": OLD_VERSION},
    )
    selected = registry.select_for_query(
        "create report",
        scope=("tenant-a", "user-a"),
        allowed_names=frozenset({"report-helper"}),
        allowed_versions={"report-helper": OLD_VERSION},
    )

    assert loaded == 1
    assert [item.skill.instructions for item in selected] == [
        "OLD IMMUTABLE INSTRUCTIONS"
    ]
    assert registry.list(
        scope=("tenant-a", "user-a"),
        allowed_versions={"report-helper": NEW_VERSION},
    ) == []


def test_bridge_registers_only_versioned_skill_tool_and_permissions_do_not_expand() -> None:
    registry = SkillRegistry()
    registry.register_scoped(
        _manifest(OLD_VERSION, "EXACT"),
        tenant_id="tenant-a",
        user_id="user-a",
    )

    class Tools:
        def __init__(self) -> None:
            self.definitions = {}

        def register(self, definition, executor) -> None:
            self.definitions[definition.name] = SimpleNamespace(
                definition=definition,
                executor=executor,
            )

    tools = Tools()
    bridge = SkillToolBridge(registry, tools)
    count = bridge.sync_all_skills(
        scope=("tenant-a", "user-a"),
        allowed_names=frozenset({"report-helper"}),
        allowed_versions={"report-helper": OLD_VERSION},
    )

    expected = skill_tool_name("report-helper", OLD_VERSION)
    assert count == 1
    assert set(tools.definitions) == {expected}
    definition = tools.definitions[expected].definition
    assert definition.required_permissions == ["knowledge:read"]
    assert "knowledge:read" not in tools.definitions


@pytest.mark.asyncio
async def test_name_version_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_name = _manifest(OLD_VERSION, "WRONG")
    wrong_name.name = "other-skill"

    class Repository:
        def __init__(self, _database):
            pass

        async def load_versions(self, **_values):
            return [wrong_name]

    import ai_gateway_core.skills.registry as registry_module

    monkeypatch.setattr(registry_module, "DatabaseSkillArtifactRepository", Repository)
    registry = SkillRegistry(database=object())
    with pytest.raises(ValueError, match="name/version binding mismatch"):
        await registry.load_versions_from_database(
            "tenant-a",
            "user-a",
            allowed_versions={"report-helper": OLD_VERSION},
        )


@pytest.mark.asyncio
async def test_exact_skill_tool_overlay_is_request_scoped_and_invokable() -> None:
    skills = SkillRegistry()
    skills.register_scoped(
        _manifest(OLD_VERSION, "EXACT REQUEST-SCOPED INSTRUCTIONS"),
        tenant_id="tenant-a",
        user_id="user-a",
    )
    global_tools = ToolRegistry()
    runtime_tools = ToolRegistry()
    SkillToolBridge(skills, runtime_tools).sync_all_skills(
        scope=("tenant-a", "user-a"),
        allowed_names=frozenset({"report-helper"}),
        allowed_versions={"report-helper": OLD_VERSION},
    )
    expected = skill_tool_name("report-helper", OLD_VERSION)
    invoker = RegistryToolInvoker(global_tools)
    user = SimpleNamespace(roles=["admin"], tier="admin")
    scoped = ToolInvocationContext(
        session_id="session-a",
        user_id="user-a",
        tenant_id="tenant-a",
        request_id="request-a",
        user=user,
        runtime_tool_registry=runtime_tools,
    )

    assert invoker.get_available_tools(scoped) == [expected]
    result = await invoker.invoke(expected, {"input": "report"}, scoped)
    assert result.success is True
    assert result.result == "EXACT REQUEST-SCOPED INSTRUCTIONS"

    unscoped = ToolInvocationContext(
        session_id="session-b",
        user_id="user-b",
        tenant_id="tenant-b",
        request_id="request-b",
        user=user,
    )
    assert expected not in invoker.get_available_tools(unscoped)
    assert global_tools.get_tool(expected) is None
