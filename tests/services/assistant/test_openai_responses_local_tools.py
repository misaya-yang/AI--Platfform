"""OpenAI Responses local Computer Use and shell translation contracts."""

from __future__ import annotations

import base64

import pytest
from assistant_service.core.providers import (
    ComputerScreenshotObservation,
    ProcessExecutionResult,
    ProviderToolContractError,
    build_openai_computer_call_output,
    build_openai_shell_call_output,
    parse_openai_computer_call,
    parse_openai_shell_call,
    validate_openai_local_tool_definition,
)


def _computer_call(**overrides):
    value = {
        "id": "item_1",
        "type": "computer_call",
        "call_id": "call_computer_1",
        "actions": [{"type": "screenshot"}],
        "status": "completed",
    }
    value.update(overrides)
    return value


def _shell_call(**overrides):
    value = {
        "id": "item_2",
        "type": "shell_call",
        "call_id": "call_shell_1",
        "action": {
            "commands": ["python -m pytest -q", "git status --short"],
            "timeout_ms": 60_000,
            "max_output_length": 4_096,
        },
        "status": "in_progress",
    }
    value.update(overrides)
    return value


def _screenshot() -> ComputerScreenshotObservation:
    encoded = base64.b64encode(b"not-a-real-png-but-valid-provider-payload").decode()
    return ComputerScreenshotObservation(image_url=f"data:image/png;base64,{encoded}")


def test_current_tool_definitions_are_explicit_and_legacy_tools_are_rejected() -> None:
    assert validate_openai_local_tool_definition({"type": "computer"}).kind == "computer"
    shell = validate_openai_local_tool_definition(
        {"type": "shell", "environment": {"type": "local"}}
    )
    assert shell.kind == "shell"
    assert shell.environment == "local"

    for tool, code in (
        ({"type": "computer_use_preview"}, "legacy_computer_use_preview_unsupported"),
        ({"type": "local_shell"}, "legacy_local_shell_unsupported"),
    ):
        with pytest.raises(ProviderToolContractError) as exc_info:
            validate_openai_local_tool_definition(tool)
        assert exc_info.value.code == code

    with pytest.raises(ProviderToolContractError) as exc_info:
        validate_openai_local_tool_definition(
            {"type": "shell", "environment": {"type": "container_auto"}}
        )
    assert exc_info.value.code == "unsupported_shell_environment"


def test_computer_call_maps_every_ga_action_to_normalized_candidates() -> None:
    plan = parse_openai_computer_call(
        _computer_call(
            actions=[
                {"type": "click", "button": "left", "x": 1, "y": 2},
                {"type": "double_click", "x": 3, "y": 4},
                {"type": "scroll", "x": 5, "y": 6, "scroll_x": -7, "scroll_y": 8},
                {"type": "type", "text": "hello"},
                {"type": "keypress", "keys": ["CTRL", "A"]},
                {"type": "drag", "path": [[1, 2], {"x": 3, "y": 4}]},
                {"type": "move", "x": 9, "y": 10},
                {"type": "wait"},
                {"type": "screenshot"},
            ]
        )
    )

    assert plan.provider_call_id == "call_computer_1"
    assert plan.provider_item_id == "item_1"
    assert [action.kind for action in plan.actions] == [
        "click",
        "double_click",
        "scroll",
        "type_text",
        "key_press",
        "drag",
        "move",
        "wait",
        "screenshot",
    ]
    assert [action.ordinal for action in plan.actions] == list(range(9))
    assert all(action.provider_call_id == plan.provider_call_id for action in plan.actions)
    assert all(action.authoritative is False for action in plan.actions)
    assert plan.authoritative is False
    assert plan.actions[2].normalized_arguments() == {
        "x": 5,
        "y": 6,
        "scroll_x": -7,
        "scroll_y": 8,
    }
    assert plan.actions[5].normalized_arguments()["path"] == [
        {"x": 1, "y": 2},
        {"x": 3, "y": 4},
    ]


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ({"type": "launch_missiles"}, "unsupported_computer_action"),
        ({"type": "click", "x": True, "y": 1}, "invalid_computer_coordinate"),
        ({"type": "drag", "path": [[1, 2]]}, "invalid_computer_drag_path"),
        ({"type": "wait", "seconds": 5}, "invalid_computer_action"),
    ],
)
def test_computer_action_validation_fails_closed(action, code) -> None:
    with pytest.raises(ProviderToolContractError) as exc_info:
        parse_openai_computer_call(_computer_call(actions=[action]))
    assert exc_info.value.code == code


def test_computer_call_rejects_legacy_preview_wire_item() -> None:
    with pytest.raises(ProviderToolContractError) as exc_info:
        parse_openai_computer_call(
            {
                "type": "computer_use_preview_call",
                "call_id": "legacy",
                "actions": [{"type": "screenshot"}],
                "status": "completed",
            }
        )
    assert exc_info.value.code == "legacy_computer_use_preview_unsupported"


def test_provider_safety_checks_require_exact_platform_approval() -> None:
    plan = parse_openai_computer_call(
        _computer_call(
            pending_safety_checks=[
                {
                    "id": "check_1",
                    "code": "sensitive_data",
                    "message": "Confirm before sending data",
                },
                {
                    "id": "check_2",
                    "code": "external_side_effect",
                    "message": "Confirm before submitting",
                },
            ]
        )
    )
    assert plan.requires_approval is True
    assert all(item.requires_platform_approval for item in plan.approval_requirements)
    assert all(item.authoritative is False for item in plan.approval_requirements)

    with pytest.raises(ProviderToolContractError) as exc_info:
        build_openai_computer_call_output(plan, _screenshot())
    assert exc_info.value.code == "provider_safety_approval_required"

    with pytest.raises(ProviderToolContractError) as exc_info:
        build_openai_computer_call_output(
            plan,
            _screenshot(),
            approved_safety_check_ids=["check_1", "unexpected"],
        )
    assert exc_info.value.code == "provider_safety_approval_mismatch"

    output = build_openai_computer_call_output(
        plan,
        _screenshot(),
        approved_safety_check_ids=["check_2", "check_1"],
    )
    assert output["call_id"] == plan.provider_call_id
    assert [item["id"] for item in output["acknowledged_safety_checks"]] == [
        "check_1",
        "check_2",
    ]


def test_computer_output_preserves_call_id_and_uses_original_screenshot() -> None:
    plan = parse_openai_computer_call(_computer_call())
    output = build_openai_computer_call_output(plan, _screenshot())
    assert output == {
        "type": "computer_call_output",
        "call_id": "call_computer_1",
        "output": {
            "type": "computer_screenshot",
            "image_url": _screenshot().image_url,
            "detail": "original",
        },
    }

    with pytest.raises(ProviderToolContractError) as exc_info:
        build_openai_computer_call_output(
            plan,
            ComputerScreenshotObservation(image_url="http://insecure.example/screen.png"),
        )
    assert exc_info.value.code == "invalid_computer_screenshot"


def test_shell_call_becomes_restricted_structured_processes(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "project"
    cwd.mkdir(parents=True)
    plan = parse_openai_shell_call(_shell_call(), workspace_root=workspace, cwd=cwd)

    assert plan.provider_call_id == "call_shell_1"
    assert plan.authoritative is False
    assert plan.requires_platform_approval is True
    assert [request.argv for request in plan.requests] == [
        ("python", "-m", "pytest", "-q"),
        ("git", "status", "--short"),
    ]
    for request in plan.requests:
        assert request.cwd == str(cwd.resolve())
        assert request.environment == ()
        assert request.inherit_environment is False
        assert request.interactive is False
        assert request.network_policy.mode == "deny"
        assert request.network_policy.allowed_domains == ()
        assert request.requires_platform_approval is True
        assert request.authoritative is False


def test_shell_network_allowlist_is_platform_supplied_not_provider_supplied(tmp_path) -> None:
    plan = parse_openai_shell_call(
        _shell_call(),
        workspace_root=tmp_path,
        cwd=".",
        network_allowlist=["API.EXAMPLE.COM", "api.example.com", "cdn.example.com"],
    )
    assert plan.requests[0].network_policy.mode == "allowlist"
    assert plan.requests[0].network_policy.allowed_domains == (
        "api.example.com",
        "cdn.example.com",
    )


def test_shell_cwd_cannot_escape_workspace_even_through_symlink(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    for cwd in (outside, workspace / "escape"):
        with pytest.raises(ProviderToolContractError) as exc_info:
            parse_openai_shell_call(_shell_call(), workspace_root=workspace, cwd=cwd)
        assert exc_info.value.code == "shell_cwd_outside_workspace"


@pytest.mark.parametrize(
    "command",
    [
        "git status && curl https://example.com",
        "cat file | grep secret",
        "FOO=secret python check.py",
        "echo ok\nwhoami",
    ],
)
def test_shell_rejects_unresolved_shell_control_and_environment_syntax(tmp_path, command) -> None:
    raw = _shell_call(
        action={"commands": [command], "timeout_ms": 1_000, "max_output_length": 1_024}
    )
    with pytest.raises(ProviderToolContractError):
        parse_openai_shell_call(raw, workspace_root=tmp_path, cwd=".")


def test_shell_rejects_legacy_local_shell_item() -> None:
    with pytest.raises(ProviderToolContractError) as exc_info:
        parse_openai_shell_call(
            {
                "type": "local_shell_call",
                "call_id": "legacy",
                "action": {"command": "ls"},
                "status": "completed",
            },
            workspace_root="/tmp/workspace",
            cwd=".",
        )
    assert exc_info.value.code == "legacy_local_shell_unsupported"


@pytest.mark.parametrize(
    ("timeout_ms", "max_output_length", "code"),
    [
        (0, 1_024, "invalid_shell_timeout"),
        (120_001, 1_024, "invalid_shell_timeout"),
        (1_000, 0, "invalid_shell_output_limit"),
        (1_000, 1_048_577, "invalid_shell_output_limit"),
    ],
)
def test_shell_enforces_local_execution_budgets(
    tmp_path, timeout_ms, max_output_length, code
) -> None:
    raw = _shell_call(
        action={
            "commands": ["git status"],
            "timeout_ms": timeout_ms,
            "max_output_length": max_output_length,
        }
    )
    with pytest.raises(ProviderToolContractError) as exc_info:
        parse_openai_shell_call(raw, workspace_root=tmp_path, cwd=".")
    assert exc_info.value.code == code


def test_shell_output_preserves_call_id_order_and_outcomes(tmp_path) -> None:
    plan = parse_openai_shell_call(_shell_call(), workspace_root=tmp_path, cwd=".")
    output = build_openai_shell_call_output(
        plan,
        [
            ProcessExecutionResult(
                command_index=1,
                stdout="",
                stderr="timed out",
                outcome="timeout",
            ),
            ProcessExecutionResult(
                command_index=0,
                stdout="passed",
                stderr="",
                outcome="exit",
                exit_code=0,
            ),
        ],
    )
    assert output == {
        "type": "shell_call_output",
        "call_id": "call_shell_1",
        "max_output_length": 4_096,
        "output": [
            {"stdout": "passed", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}},
            {"stdout": "", "stderr": "timed out", "outcome": {"type": "timeout"}},
        ],
    }


def test_shell_output_rejects_missing_or_oversized_results(tmp_path) -> None:
    plan = parse_openai_shell_call(_shell_call(), workspace_root=tmp_path, cwd=".")
    with pytest.raises(ProviderToolContractError) as exc_info:
        build_openai_shell_call_output(plan, [])
    assert exc_info.value.code == "shell_result_count_mismatch"

    with pytest.raises(ProviderToolContractError) as exc_info:
        build_openai_shell_call_output(
            plan,
            [
                ProcessExecutionResult(0, "x" * 4_097, "", "exit", 0),
                ProcessExecutionResult(1, "", "", "exit", 0),
            ],
        )
    assert exc_info.value.code == "shell_result_exceeds_output_limit"
