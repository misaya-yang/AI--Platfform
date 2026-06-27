from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_golden import main as eval_golden_main
from src.services.eval.golden import apply_gate, evaluate_cases, load_jsonl, validate_cases

GOLDEN = Path("tests/fixtures/eval/golden/assistant_regression_v1.jsonl")


def test_assistant_golden_fixture_validates_and_has_seed_coverage() -> None:
    cases = load_jsonl(GOLDEN)
    result = validate_cases(cases)

    assert result["valid"] is True
    assert result["case_count"] >= 10
    assert any(case["metadata"].get("critical") is True for case in cases)
    assert all("expected_trajectory" in case for case in cases)


def test_offline_gate_passes_seed_fixture_without_model_calls(tmp_path: Path) -> None:
    output = tmp_path / "latest.json"
    markdown = tmp_path / "latest.md"

    exit_code = eval_golden_main(
        [
            "gate",
            str(GOLDEN),
            "--output",
            str(output),
            "--markdown",
            str(markdown),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["gate"]["status"] == "pass"
    assert payload["metrics"]["overall_score"] >= 0.85
    assert "Eval Regression Gate" in markdown.read_text(encoding="utf-8")


def test_gate_fails_low_quality_metrics() -> None:
    gate = apply_gate(
        {
            "overall_score": 0.5,
            "trajectory_pass_rate": 0.5,
            "critical_pass_rate": 0.5,
        }
    )

    assert gate["status"] == "fail"
    assert len(gate["failures"]) == 3


def test_golden_redaction_regression_is_a_failure() -> None:
    cases = [
        {
            "case_id": "unsafe",
            "input": {},
            "expected_output": {"contains": "safe"},
            "expected_trajectory": {"required_span_kinds": ["model_invocation"]},
            "assertions": [],
            "metadata": {
                "critical": True,
                "replay": {
                    "status": "succeeded",
                    "output_preview": "safe Authorization: Bearer raw-token",
                    "span_kinds": ["model_invocation"],
                },
            },
        }
    ]

    metrics = evaluate_cases(cases)

    assert metrics["critical_pass_rate"] == 0.0
    assert metrics["cases"][0]["passed"] is False
    assert "sensitive replay payload" in metrics["cases"][0]["failures"]
