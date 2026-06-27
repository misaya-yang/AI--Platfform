from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_GATE_THRESHOLDS = {
    "overall_score": 0.85,
    "trajectory_pass_rate": 0.95,
    "critical_pass_rate": 1.0,
    "baseline_tolerance": 0.02,
}

SENSITIVE_PATTERNS = [
    re.compile(r"authorization\s*[:=]\s*bearer\s+[a-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\bcookie\s*[:=]\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^,\s]+", re.IGNORECASE),
]


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_no}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Golden case at line {line_no} must be an object")
        rows.append(payload)
    return rows


def validate_case(case: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("case_id", "input", "expected_output", "expected_trajectory", "assertions", "metadata"):
        if key not in case:
            errors.append(f"missing {key}")
    if not isinstance(case.get("case_id"), str) or not case.get("case_id"):
        errors.append("case_id must be a non-empty string")
    for key in ("input", "expected_output", "expected_trajectory", "metadata"):
        if key in case and not isinstance(case.get(key), dict):
            errors.append(f"{key} must be an object")
    if "assertions" in case and not isinstance(case.get("assertions"), list):
        errors.append("assertions must be a list")
    split = case.get("split", "regression")
    if not isinstance(split, str) or not split:
        errors.append("split must be a non-empty string")
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    if metadata.get("critical") is not None and not isinstance(metadata.get("critical"), bool):
        errors.append("metadata.critical must be boolean when present")
    return errors


def validate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("case_id") or f"line-{index}")
        case_errors = validate_case(case)
        if case_id in seen:
            case_errors.append("duplicate case_id")
        seen.add(case_id)
        if case_errors:
            errors.append({"case_id": case_id, "errors": case_errors})
    return {"valid": not errors, "case_count": len(cases), "errors": errors}


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_split: dict[str, int] = {}
    by_difficulty: dict[str, int] = {}
    critical = 0
    tags: dict[str, int] = {}
    for case in cases:
        split = str(case.get("split") or "regression")
        by_split[split] = by_split.get(split, 0) + 1
        metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
        difficulty = str(metadata.get("difficulty") or "unspecified")
        by_difficulty[difficulty] = by_difficulty.get(difficulty, 0) + 1
        if metadata.get("critical") is True:
            critical += 1
        for tag in metadata.get("tags") or []:
            if isinstance(tag, str) and tag:
                tags[tag] = tags.get(tag, 0) + 1
    return {
        "case_count": len(cases),
        "critical_count": critical,
        "by_split": by_split,
        "by_difficulty": by_difficulty,
        "tags": tags,
    }


def _contains_sensitive(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def _case_replay(case: dict[str, Any]) -> dict[str, Any]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    return replay


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    replay = _case_replay(case)
    output = str(replay.get("output_preview") or "")
    spans = replay.get("span_kinds") if isinstance(replay.get("span_kinds"), list) else []
    status = str(replay.get("status") or "succeeded")
    expected_output = case.get("expected_output") if isinstance(case.get("expected_output"), dict) else {}
    expected_trajectory = (
        case.get("expected_trajectory") if isinstance(case.get("expected_trajectory"), dict) else {}
    )
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    required_text = str(expected_output.get("contains") or expected_output.get("output_preview") or "").strip()
    required_spans = [
        str(item)
        for item in expected_trajectory.get("required_span_kinds", [])
        if isinstance(item, str) and item
    ]

    failures: list[str] = []
    if status != "succeeded":
        failures.append(f"status={status}")
    if required_text and required_text.lower() not in output.lower():
        failures.append(f"missing expected text {required_text!r}")
    missing_spans = [span for span in required_spans if span not in spans]
    if missing_spans:
        failures.append(f"missing spans {','.join(missing_spans)}")
    if _contains_sensitive(replay):
        failures.append("sensitive replay payload")

    trajectory_pass = not missing_spans
    score_components = [
        1.0 if status == "succeeded" else 0.0,
        1.0 if not required_text or required_text.lower() in output.lower() else 0.0,
        1.0 if trajectory_pass else 0.0,
        0.0 if _contains_sensitive(replay) else 1.0,
    ]
    score = sum(score_components) / len(score_components)
    return {
        "case_id": case.get("case_id"),
        "score": round(score, 4),
        "passed": not failures,
        "critical": metadata.get("critical") is True,
        "trajectory_pass": trajectory_pass,
        "failures": failures,
    }


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    case_results = [evaluate_case(case) for case in cases]
    total = len(case_results)
    if total == 0:
        return {
            "overall_score": 0.0,
            "pass_rate": 0.0,
            "trajectory_pass_rate": 0.0,
            "critical_pass_rate": 0.0,
            "cases": [],
        }
    critical_cases = [case for case in case_results if case["critical"]]
    return {
        "overall_score": round(sum(case["score"] for case in case_results) / total, 4),
        "pass_rate": round(sum(1 for case in case_results if case["passed"]) / total, 4),
        "trajectory_pass_rate": round(
            sum(1 for case in case_results if case["trajectory_pass"]) / total,
            4,
        ),
        "critical_pass_rate": round(
            sum(1 for case in critical_cases if case["passed"]) / max(len(critical_cases), 1),
            4,
        ),
        "cases": case_results,
    }


def apply_gate(
    metrics: dict[str, Any],
    *,
    thresholds: dict[str, float] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gate_thresholds = {**DEFAULT_GATE_THRESHOLDS, **(thresholds or {})}
    failures: list[str] = []
    overall_score = float(metrics.get("overall_score") or metrics.get("average_score") or 0.0)
    trajectory_pass_rate = float(metrics.get("trajectory_pass_rate") or 0.0)
    critical_pass_rate = float(metrics.get("critical_pass_rate") or metrics.get("pass_rate") or 0.0)

    if overall_score < gate_thresholds["overall_score"]:
        failures.append(f"overall_score {overall_score:.4f} < {gate_thresholds['overall_score']:.4f}")
    if trajectory_pass_rate < gate_thresholds["trajectory_pass_rate"]:
        failures.append(
            f"trajectory_pass_rate {trajectory_pass_rate:.4f} < {gate_thresholds['trajectory_pass_rate']:.4f}"
        )
    if critical_pass_rate < gate_thresholds["critical_pass_rate"]:
        failures.append(
            f"critical_pass_rate {critical_pass_rate:.4f} < {gate_thresholds['critical_pass_rate']:.4f}"
        )
    if baseline_metrics:
        baseline_score = float(
            baseline_metrics.get("overall_score") or baseline_metrics.get("average_score") or 0.0
        )
        allowed = baseline_score - gate_thresholds["baseline_tolerance"]
        if overall_score < allowed:
            failures.append(f"candidate score {overall_score:.4f} < baseline tolerance {allowed:.4f}")

    return {
        "status": "fail" if failures else "pass",
        "thresholds": gate_thresholds,
        "metrics": metrics,
        "failures": failures,
    }


def write_gate_report(result: dict[str, Any], json_path: str | Path, markdown_path: str | Path) -> None:
    json_target = Path(json_path)
    md_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    md_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    failures = result.get("gate", {}).get("failures") or []
    lines = [
        "# Eval Regression Gate",
        "",
        f"Status: `{result.get('gate', {}).get('status', 'unknown')}`",
        f"Cases: `{result.get('summary', {}).get('case_count', 0)}`",
        f"Overall score: `{result.get('metrics', {}).get('overall_score', 0)}`",
        f"Trajectory pass rate: `{result.get('metrics', {}).get('trajectory_pass_rate', 0)}`",
        f"Critical pass rate: `{result.get('metrics', {}).get('critical_pass_rate', 0)}`",
        "",
        "## Failures",
        "",
    ]
    lines.extend([f"- {failure}" for failure in failures] or ["- None"])
    md_target.write_text("\n".join(lines) + "\n", encoding="utf-8")
