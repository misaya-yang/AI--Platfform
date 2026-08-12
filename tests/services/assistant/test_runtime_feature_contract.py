from __future__ import annotations

import hashlib
import json

import pytest


def test_runtime_feature_contract_resolves_defaults_and_has_stable_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-shared-secret")
    from assistant_service.main import _resolved_runtime_feature_contract

    names = (
        "ASSISTANT_GATEWAY_ENABLED",
        "ASSISTANT_RUNTIME_CONTEXT_V2",
        "ASSISTANT_RUNTIME_MEMORY_V2",
        "ASSISTANT_RUNTIME_SKILLS",
        "ASSISTANT_STAGED_COMPACTION_ENABLED",
        "ASSISTANT_SUBAGENTS_ENABLED",
        "ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    contract = _resolved_runtime_feature_contract()

    assert contract["features"] == {
        "gateway": True,
        "runtime_context_v2": True,
        "runtime_memory_v2": True,
        "runtime_skills": True,
        "staged_compaction": False,
        "subagents": False,
        "tool_output_spill": True,
    }
    encoded = json.dumps(contract["features"], sort_keys=True, separators=(",", ":")).encode()
    assert contract["sha256"] == hashlib.sha256(encoded).hexdigest()


def test_runtime_feature_contract_changes_when_operator_enables_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-shared-secret")
    from assistant_service.main import _resolved_runtime_feature_contract

    monkeypatch.setenv("ASSISTANT_SUBAGENTS_ENABLED", "false")
    before = _resolved_runtime_feature_contract()
    monkeypatch.setenv("ASSISTANT_SUBAGENTS_ENABLED", "true")
    after = _resolved_runtime_feature_contract()

    assert before["features"]["subagents"] is False
    assert after["features"]["subagents"] is True
    assert before["sha256"] != after["sha256"]


def test_runtime_adapter_skills_default_matches_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.runtime.compat.runtime_adapter import AssistantRuntimeAdapter

    monkeypatch.delenv("ASSISTANT_RUNTIME_SKILLS", raising=False)
    monkeypatch.setenv("ASSISTANT_AGENT_PLUGIN_PATHS", "")
    adapter = AssistantRuntimeAdapter.from_env(database=None)

    assert adapter.features.skills is True
