"""OpenAI GA computer/shell contract tests for OS-A21 and OS-A22.

Provider calls are proposals, never authoritative local execution receipts.
Every provider safety check must map to an exact platform approval before the
next screenshot/output can be sent.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from assistant_service.core.providers.openai_responses_tools import (
    ComputerScreenshotObservation,
    ProviderToolContractError,
    build_openai_computer_call_output,
    parse_openai_computer_call,
    parse_openai_shell_call,
    validate_openai_local_tool_definition,
)


def test_pending_safety_checks_become_exact_platform_approval_requirements(
    provider_call_fixture,
) -> None:
    raw = provider_call_fixture["valid_with_safety_checks"]

    plan = parse_openai_computer_call(raw)

    assert plan.authoritative is False
    assert plan.requires_approval is True
    assert [requirement.check_id for requirement in plan.approval_requirements] == [
        "safety_001"
    ]


def test_provider_safety_check_cannot_be_auto_confirmed(provider_call_fixture) -> None:
    plan = parse_openai_computer_call(provider_call_fixture["valid_with_safety_checks"])
    observation = ComputerScreenshotObservation(
        image_url="data:image/png;base64,ZmFrZS1vZmZsaW5lLXBuZw==",
        observation_ref="observation-002",
    )

    with pytest.raises(ProviderToolContractError) as missing:
        build_openai_computer_call_output(plan, observation)
    assert missing.value.code == "provider_safety_approval_required"

    with pytest.raises(ProviderToolContractError) as mismatch:
        build_openai_computer_call_output(
            plan,
            observation,
            approved_safety_check_ids={"safety-from-a-different-call"},
        )
    assert mismatch.value.code == "provider_safety_approval_mismatch"


def test_exact_approved_safety_check_ids_are_returned_to_same_call(provider_call_fixture) -> None:
    plan = parse_openai_computer_call(provider_call_fixture["valid_with_safety_checks"])

    output = build_openai_computer_call_output(
        plan,
        ComputerScreenshotObservation(
            image_url="data:image/png;base64,ZmFrZS1vZmZsaW5lLXBuZw==",
            observation_ref="observation-002",
        ),
        approved_safety_check_ids={"safety_001"},
    )

    assert output["type"] == "computer_call_output"
    assert output["call_id"] == plan.provider_call_id
    assert [item["id"] for item in output["acknowledged_safety_checks"]] == ["safety_001"]


def test_provider_cannot_smuggle_additional_safety_check_after_approval(
    provider_call_fixture,
) -> None:
    raw = copy.deepcopy(provider_call_fixture["valid_with_safety_checks"])
    raw["pending_safety_checks"].append(
        {"id": "safety_002", "code": "new_target", "message": "A new target appeared"}
    )
    plan = parse_openai_computer_call(raw)

    with pytest.raises(ProviderToolContractError) as mismatch:
        build_openai_computer_call_output(
            plan,
            ComputerScreenshotObservation(image_url="data:image/png;base64,ZmFrZQ=="),
            approved_safety_check_ids={"safety_001"},
        )
    assert mismatch.value.code == "provider_safety_approval_mismatch"


def test_unknown_computer_action_fails_closed(provider_call_fixture) -> None:
    with pytest.raises(ProviderToolContractError) as error:
        parse_openai_computer_call(provider_call_fixture["unknown_action"])

    assert error.value.code == "unsupported_computer_action"


@pytest.mark.parametrize(
    ("definition", "expected_code"),
    [
        ({"type": "computer_use_preview"}, "legacy_computer_use_preview_unsupported"),
        ({"type": "local_shell"}, "legacy_local_shell_unsupported"),
    ],
)
def test_legacy_local_tool_definitions_are_rejected(
    definition: dict[str, str],
    expected_code: str,
) -> None:
    with pytest.raises(ProviderToolContractError) as error:
        validate_openai_local_tool_definition(definition)

    assert error.value.code == expected_code


def test_legacy_preview_call_is_rejected(provider_call_fixture) -> None:
    with pytest.raises(ProviderToolContractError) as error:
        parse_openai_computer_call(provider_call_fixture["legacy_preview"])

    assert error.value.code == "legacy_computer_use_preview_unsupported"


def test_local_shell_cwd_must_remain_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw = {
        "type": "shell_call",
        "id": "shell-001",
        "call_id": "shell-001",
        "status": "completed",
        "action": {
            "commands": ["python -m pytest -q"],
            "timeout_ms": 30_000,
            "max_output_length": 100_000,
        },
    }

    with pytest.raises(ProviderToolContractError) as error:
        parse_openai_shell_call(
            raw,
            workspace_root=workspace,
            cwd=tmp_path,
            network_allowlist=(),
        )

    assert error.value.code == "shell_cwd_outside_workspace"
