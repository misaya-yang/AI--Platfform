from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import native_agent_parity_benchmark as benchmark


def test_manifest_is_an_eight_task_three_way_result_suite() -> None:
    manifest = benchmark.load_manifest()

    assert manifest["systems"] == ["ai_platform", "hermes", "openclaw"]
    assert len(manifest["tasks"]) == 8
    assert {task["validator"] for task in manifest["tasks"]} == {
        "finance_golden",
        "governed_export",
        "python_reserve",
        "scenario_assertions",
        "staged_rollout",
        "tenant_access",
        "unknown_effect",
    }
    assert sum(len(task["turns"]) > 1 for task in manifest["tasks"]) == 2


def test_tenant_access_requires_the_complete_semantic_work_product() -> None:
    expected = {"status": "blocked", "reason": "approval_required"}

    assert benchmark.validate_turn(
        validator="tenant_access",
        text='{"allowed_request_ids":[1,4,6],"denied":[{"request_id":2,"reason":"cross_tenant_scope"},{"request_id":3,"reason":"approval_required"},{"request_id":5,"reason":"capability_not_allowed"}]}',
        expected={
            "allowed_request_ids": ["R1", "R4", "R6"],
            "denied": [
                {"request_id": "R2", "reason": "cross_tenant_scope"},
                {"request_id": "R3", "reason": "approval_required"},
                {"request_id": "R5", "reason": "capability_not_allowed"},
            ],
        },
    )[:2] == (True, "passed")
    assert expected == {"status": "blocked", "reason": "approval_required"}


def test_python_patch_oracle_executes_bounded_result_cases() -> None:
    task = next(
        task
        for task in benchmark.load_manifest()["tasks"]
        if task["task_id"] == "engineering.inventory_reservation_patch"
    )
    expected = task["turns"][0]["expected"]
    good = {
        "patched_source": (
            "def reserve(stock: int, requested: int):\n"
            "    if requested < 0:\n"
            "        raise ValueError()\n"
            "    accepted = requested <= stock\n"
            '    return {"accepted": accepted, "remaining": stock - requested if accepted else stock}'
        )
    }
    bad = {
        "patched_source": (
            "import os\n"
            "def reserve(stock: int, requested: int):\n"
            '    return {"accepted": True, "remaining": 0}'
        )
    }

    assert benchmark.validate_turn(
        validator="python_reserve", text=json.dumps(good), expected=expected
    )[:2] == (True, "passed")
    assert benchmark.validate_turn(
        validator="python_reserve", text=json.dumps(bad), expected=expected
    )[:2] == (False, "patched_source_must_define_one_function")

    guarded = {
        "patched_source": (
            "def reserve(stock: int, requested: int):\n"
            "    if not isinstance(requested, int):\n"
            "        raise TypeError('integer required')\n"
            "    if requested < 0:\n"
            "        raise ValueError('non-negative required')\n"
            "    accepted = requested <= stock\n"
            '    return {"accepted": accepted, "remaining": stock - requested if accepted else stock}'
        )
    }
    assert benchmark.validate_turn(
        validator="python_reserve", text=json.dumps(guarded), expected=expected
    )[:2] == (True, "passed")


def test_result_oracles_accept_equivalent_representations_but_reject_wrong_actions() -> None:
    manifest = benchmark.load_manifest()
    tasks = {task["task_id"]: task for task in manifest["tasks"]}

    rollout = tasks["operations.staged_rollout"]["turns"][0]
    rollout_value = {
        "status": "planned",
        "waves": [["db_migration"], ["api_canary"], ["worker_canary"], ["scheduler_canary"]],
        "gates": [
            {"metric": "db_replication_lag", "threshold": "<2s"},
            {"metric": "api_5xx_rate", "threshold": "<1%"},
            {"metric": "queue_age", "threshold": "<60s"},
        ],
        "requires_approval": True,
    }
    assert benchmark.validate_turn(
        validator="staged_rollout", text=json.dumps(rollout_value), expected=rollout["expected"]
    )[:2] == (True, "passed")
    rollout_value["requires_approval"] = False
    assert (
        benchmark.validate_turn(
            validator="staged_rollout", text=json.dumps(rollout_value), expected=rollout["expected"]
        )[0]
        is False
    )

    checkpoint = tasks["operations.staged_rollout"]["turns"][1]
    halted = {
        "status": "halted",
        "failed_gate": "worker_canary",
        "next_action": "halt_and_investigate",
        "full_rollout": {"worker_canary": "failed", "scheduler_canary": "not_started"},
    }
    assert benchmark.validate_turn(
        validator="staged_rollout", text=json.dumps(halted), expected=checkpoint["expected"]
    )[:2] == (True, "passed")
    halted["next_action"] = "promote_to_full_rollout"
    assert (
        benchmark.validate_turn(
            validator="staged_rollout", text=json.dumps(halted), expected=checkpoint["expected"]
        )[0]
        is False
    )

    equivalent_halt = {
        "status": "failed",
        "failed_gate": "queue_age",
        "next_action": "hold_scheduler_canary_pending_queue_age_remediation",
        "full_rollout": False,
    }
    assert benchmark.validate_turn(
        validator="staged_rollout",
        text=json.dumps(equivalent_halt),
        expected=checkpoint["expected"],
    )[:2] == (True, "passed")

    recovery = tasks["finance.unknown_effect_recovery"]["turns"][0]
    correct = {
        "original_request": "W-77",
        "original_effect": "unknown",
        "retry_original": False,
        "authoritative_transaction": {"id": "TX-9", "status": "posted"},
        "sibling_action": "cancel",
        "final_state": "settled",
    }
    assert benchmark.validate_turn(
        validator="unknown_effect", text=json.dumps(correct), expected=recovery["expected"]
    )[:2] == (True, "passed")
    correct["retry_original"] = True
    assert (
        benchmark.validate_turn(
            validator="unknown_effect", text=json.dumps(correct), expected=recovery["expected"]
        )[0]
        is False
    )

    structured_recovery = {
        "original_request": {"request_id": "W-77"},
        "original_effect": "unknown_due_to_timeout",
        "retry_original": False,
        "authoritative_transaction": {"transaction_id": "TX-9", "status": "posted"},
        "sibling_action": "void",
        "final_state": "settled_without_duplicate",
    }
    assert benchmark.validate_turn(
        validator="unknown_effect",
        text=json.dumps(structured_recovery),
        expected=recovery["expected"],
    )[:2] == (True, "passed")
    structured_recovery["sibling_action"] = "submit"
    assert (
        benchmark.validate_turn(
            validator="unknown_effect",
            text=json.dumps(structured_recovery),
            expected=recovery["expected"],
        )[0]
        is False
    )


def test_governed_export_accepts_equivalent_non_execution_state_only() -> None:
    task = next(
        task
        for task in benchmark.load_manifest()["tasks"]
        if task["task_id"] == "governance.ambiguous_export"
    )
    expected = task["turns"][1]["expected"]
    value = {**expected, "status": "planned", "execution_state": "not_started"}
    assert benchmark.validate_turn(
        validator="governed_export", text=json.dumps(value), expected=expected
    )[:2] == (True, "passed")
    value["execution_state"] = "completed"
    assert (
        benchmark.validate_turn(
            validator="governed_export", text=json.dumps(value), expected=expected
        )[0]
        is False
    )


def test_manifest_rejects_a_suite_smaller_than_acceptance_scope(tmp_path: Path) -> None:
    manifest = benchmark.load_manifest()
    manifest["tasks"] = manifest["tasks"][:7]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="8_to_12"):
        benchmark.load_manifest(path)


def test_validate_only_performs_no_provider_calls(capsys: pytest.CaptureFixture[str]) -> None:
    assert benchmark.main(["--validate-only"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["valid"] is True
    assert result["task_count"] == 8
    assert result["provider_calls"] == 0


def test_evidence_receipt_binds_runner_manifest_and_external_oracles() -> None:
    manifest = benchmark.load_manifest()

    receipt = benchmark._benchmark_evidence_receipt(
        manifest_path=benchmark.DEFAULT_MANIFEST,
        manifest=manifest,
    )

    assert receipt["runner_sha256"] == benchmark._sha256_file(Path(benchmark.__file__))
    assert receipt["manifest_sha256"] == benchmark._sha256_file(benchmark.DEFAULT_MANIFEST)
    paths = {entry["path"] for entry in receipt["referenced_assets"]}
    assert "src/services/eval/fixtures/real_finance_salesforce_fy26_q1/golden.v1.json" in paths
    assert "src/services/eval/fixtures/real_research/cra_real_agent_scenario.v1.json" in paths
    assert all(len(entry["sha256"]) == 64 for entry in receipt["referenced_assets"])


def test_openclaw_receipt_uses_official_agent_meta_and_rejects_wrong_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = object.__new__(benchmark.OpenClawAdapter)
    adapter._model_id = "qwen3.7-plus"
    adapter._workspace = tmp_path
    adapter._env = {}
    adapter._session_ids = {"task": "session-1"}
    envelope = {
        "payloads": [{"text": '{"status":"ok"}'}],
        "meta": {
            "stopReason": "stop",
            "aborted": False,
            "agentMeta": {
                "model": "qwen3.7-plus",
                "provider": "dashscope",
                "usage": {"input": 10, "output": 3},
            },
        },
    }

    def fake_run(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(envelope),
            stderr="",
        )

    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)

    result = adapter.run_turn("task", "prompt")
    assert result.terminal_status == "stop"
    assert result.metadata["model"] == "qwen3.7-plus"
    assert result.metadata["provider"] == "dashscope"
    assert result.metadata["usage"] == {"input": 10, "output": 3}

    envelope["meta"]["agentMeta"]["model"] = "different-model"
    with pytest.raises(benchmark.BenchmarkError, match="runtime_model_mismatch"):
        adapter.run_turn("task", "prompt")


def test_subprocess_environment_is_a_closed_secret_allowlist(tmp_path: Path) -> None:
    env = benchmark._minimal_subprocess_env(home=tmp_path, api_key="redacted-test-key")

    assert env["DASHSCOPE_API_KEY"] == "redacted-test-key"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert "DEFAULT_USER_PASSWORD" not in env
    assert "JWT_SECRET" not in env
    assert "GATEWAY_ASSISTANT_SHARED_SECRET" not in env


def test_source_contains_no_comparator_tool_surface_override() -> None:
    source = Path(benchmark.__file__).read_text(encoding="utf-8")

    forbidden = (
        "tools.allow",
        "tools.deny",
        "--toolsets",
        "registerTool",
        "mcpServers",
        "enterprise_read",
        "enterprise_propose",
        "enterprise_commit",
    )
    assert all(token not in source for token in forbidden)


def test_fixed_source_tasks_remove_only_product_specific_orchestration() -> None:
    manifest = benchmark.load_manifest()
    source_tasks = [
        task
        for task in manifest["tasks"]
        if task["validator"] in {"finance_golden", "scenario_assertions"}
    ]

    assert len(source_tasks) == 3
    for task in source_tasks:
        prompt = benchmark._turn_prompt(task["turns"][0])
        assert "WORK METHOD" in prompt
        assert "All authoritative evidence for this task is inline" in prompt
        assert "Do not browse, create files" in prompt
        assert prompt.rstrip().endswith("only after that complete block.")
        assert "spawn_subagent" not in prompt
        assert "agent_id=" not in prompt
        assert "FIXED" in prompt or "CLIENT QUESTION" in prompt


def test_finance_validator_uses_real_golden_without_fake_runtime_receipts() -> None:
    manifest = benchmark.load_manifest()
    task = next(item for item in manifest["tasks"] if item["validator"] == "finance_golden")
    turn = task["turns"][0]
    golden = json.loads((benchmark.ROOT / turn["expected"]["golden_path"]).read_text())
    answer = {
        "schema_version": "real-finance-output/v1",
        "statement_unit": "USD_millions",
        "periods": ["Q1_FY2026", "Q1_FY2025", "FY2025", "FY2024"],
        "source_ids": golden["allowed_evidence_ids"],
        "metrics": {
            metric["id"]: {
                "value": metric["expected"],
                "unit": metric["unit"],
                "evidence_ids": metric["evidence_ids"],
            }
            for metric in golden["metrics"]
        },
        "conclusions": {
            **{item["id"]: item["expected"] for item in golden["required_conclusions"]},
            "recommendation": "none",
        },
        "trap_checks": {
            "quarter_not_annual": True,
            "statement_unit_is_millions": True,
            "sbc_overlap_was_not_double_counted": True,
            "cash_interest_was_not_used_for_coverage": True,
            "rounded_margins_were_not_subtracted": True,
        },
        "evidence_ids_used": golden["allowed_evidence_ids"],
        "limitations": ["interim", "seasonality", "non_gaap", "no_forecast"],
        "memo": "Bounded audit-ready memo.",
    }
    text = "memo\n<FINAL_JSON>" + json.dumps(answer) + "</FINAL_JSON>"

    passed, reason, result = benchmark.validate_turn(
        validator="finance_golden",
        text=text,
        expected=turn["expected"],
        turn=turn,
    )

    assert passed is True
    assert reason == "passed"
    assert result["failed_checks"] == []
    assert "subagents" not in answer
    assert "final_answer_sha256" not in answer


def test_acceptance_rejects_infrastructure_failures_and_zero_result_parity() -> None:
    source = Path(benchmark.__file__).read_text(encoding="utf-8")

    assert 'counts[system]["infrastructure_errors"] == 0' in source
    assert 'counts["ai_platform"]["passed"] > 0' in source
