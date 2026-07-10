from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_gateway_core.security import redact_trace_text

DEFAULT_GATE_THRESHOLDS = {
    "overall_score": 0.85,
    "trajectory_pass_rate": 0.95,
    "critical_pass_rate": 1.0,
    "baseline_tolerance": 0.02,
}

SUPPORTED_ASSERTIONS = {
    "output_contains",
    "required_span_kind",
    "no_sensitive_output",
    "latency_ms_lt",
    "failure_mode_absent",
}


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


def load_observations(path: str | Path) -> dict[str, dict[str, Any]]:
    observations: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(load_jsonl(path), start=1):
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"Observation at line {index} must have a non-empty case_id")
        if case_id in observations:
            raise ValueError(f"Duplicate observation case_id {case_id!r}")
        replay = row.get("replay")
        if not isinstance(replay, dict) or not replay:
            raise ValueError(f"Observation {case_id!r} must have a non-empty replay object")
        observations[case_id] = replay
    return observations


def validate_observations(
    cases: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    case_ids = {
        str(case.get("case_id"))
        for case in cases
        if isinstance(case.get("case_id"), str) and case.get("case_id")
    }
    observation_ids = set(observations)
    errors: list[dict[str, Any]] = []
    for case_id in sorted(case_ids - observation_ids):
        errors.append({"case_id": case_id, "errors": ["missing replay observation"]})
    for case_id in sorted(observation_ids - case_ids):
        errors.append({"case_id": case_id, "errors": ["observation has no expectation"]})
    for case_id in sorted(case_ids & observation_ids):
        replay = observations.get(case_id)
        replay_errors: list[str] = []
        if not isinstance(replay, dict) or not replay:
            replay_errors.append("replay observation must be a non-empty object")
        elif not isinstance(replay.get("status"), str) or not replay.get("status"):
            replay_errors.append("replay observation must have a non-empty status")
        if replay_errors:
            errors.append({"case_id": case_id, "errors": replay_errors})
    return {
        "valid": not errors,
        "case_count": len(case_ids),
        "observation_count": len(observation_ids),
        "joined_count": len(case_ids & observation_ids),
        "errors": errors,
    }


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
    expected_trajectory = case.get("expected_trajectory")
    if (
        isinstance(expected_trajectory, dict)
        and "runtime" in expected_trajectory
        and not isinstance(expected_trajectory.get("runtime"), dict)
    ):
        errors.append("expected_trajectory.runtime must be an object")
    if "assertions" in case and not isinstance(case.get("assertions"), list):
        errors.append("assertions must be a list")
    elif isinstance(case.get("assertions"), list):
        for index, assertion in enumerate(case["assertions"], start=1):
            if not isinstance(assertion, dict):
                errors.append(f"assertions[{index}] must be an object")
                continue
            assertion_type = assertion.get("type")
            if assertion_type not in SUPPORTED_ASSERTIONS:
                errors.append(f"assertions[{index}] has unsupported type {assertion_type!r}")
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
    return redact_trace_text(text) != text


def _case_replay(case: dict[str, Any]) -> dict[str, Any]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    return replay


def _failed_case_result(case: dict[str, Any], failure: str) -> dict[str, Any]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    return {
        "case_id": case.get("case_id"),
        "score": 0.0,
        "passed": False,
        "critical": metadata.get("critical") is True,
        "trajectory_pass": False,
        "failures": [failure],
    }


def _evaluate_assertions(assertions: list[Any], replay: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    output = str(replay.get("output_preview") or "")
    spans = replay.get("span_kinds") if isinstance(replay.get("span_kinds"), list) else []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            failures.append("invalid assertion")
            continue
        assertion_type = str(assertion.get("type") or "")
        value = assertion.get("value")
        if assertion_type == "output_contains":
            needle = str(value or "").strip()
            if not needle or needle.lower() not in output.lower():
                failures.append(f"output_contains missing {needle!r}")
        elif assertion_type == "required_span_kind":
            span_kind = str(value or "").strip()
            if not span_kind or span_kind not in spans:
                failures.append(f"required_span_kind missing {span_kind!r}")
        elif assertion_type == "no_sensitive_output":
            if _contains_sensitive(replay):
                failures.append("no_sensitive_output detected sensitive replay payload")
        elif assertion_type == "latency_ms_lt":
            try:
                limit = int(value)
                actual = int(replay.get("total_latency_ms"))
            except (TypeError, ValueError):
                failures.append("latency_ms_lt requires numeric evidence")
            else:
                if actual >= limit:
                    failures.append(f"latency_ms_lt {actual} >= {limit}")
        elif assertion_type == "failure_mode_absent":
            expected_absent = str(value or "").strip()
            observed_modes: list[Any] = []
            evidence_present = False
            plural_modes = replay.get("failure_modes")
            if isinstance(plural_modes, list):
                observed_modes.extend(plural_modes)
                evidence_present = True
            scalar_mode = replay.get("failure_mode")
            if isinstance(scalar_mode, str) and scalar_mode.strip():
                observed_modes.append(scalar_mode)
                evidence_present = True
            if not evidence_present:
                failures.append("failure_mode_absent missing evidence")
            elif expected_absent and expected_absent in observed_modes:
                failures.append(f"failure_mode_absent observed {expected_absent!r}")
        else:
            failures.append(f"unsupported assertion type {assertion_type!r}")
    return failures


def _evaluate_runtime_expectations(case: dict[str, Any], replay: dict[str, Any]) -> list[str]:
    expected_trajectory = (
        case.get("expected_trajectory") if isinstance(case.get("expected_trajectory"), dict) else {}
    )
    runtime = (
        expected_trajectory.get("runtime")
        if isinstance(expected_trajectory.get("runtime"), dict)
        else {}
    )
    failures: list[str] = []
    for key, expected in runtime.items():
        if key == "expected_exit_reason":
            actual = replay.get("exit_reason")
            if actual != expected:
                failures.append(f"expected_exit_reason expected {expected!r}, got {actual!r}")
        elif key == "requires_gateway_decision":
            if expected is True and not replay.get("gateway_decision"):
                failures.append("requires_gateway_decision missing gateway_decision")
        elif key == "requires_arguments_hash":
            if expected is True and replay.get("arguments_hash_present") is not True:
                failures.append("requires_arguments_hash missing arguments_hash_present")
        elif key == "requires_sandbox_profile":
            actual = replay.get("sandbox_profile")
            if actual != expected:
                failures.append(f"requires_sandbox_profile expected {expected!r}, got {actual!r}")
        elif replay.get(key) != expected:
            failures.append(f"{key} expected {expected!r}, got {replay.get(key)!r}")
    return failures


def evaluate_case(
    case: dict[str, Any],
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay = observation if isinstance(observation, dict) else _case_replay(case)
    if not replay:
        return _failed_case_result(case, "missing replay observation")
    output = str(replay.get("output_preview") or "")
    spans = replay.get("span_kinds") if isinstance(replay.get("span_kinds"), list) else []
    status = str(replay.get("status") or "")
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

    assertions = case.get("assertions") if isinstance(case.get("assertions"), list) else []
    failures = _evaluate_assertions(assertions, replay)
    runtime_failures = _evaluate_runtime_expectations(case, replay)
    failures.extend(runtime_failures)
    if not status:
        failures.append("missing replay status")
    elif status != "succeeded":
        failures.append(f"status={status}")
    if required_text and required_text.lower() not in output.lower():
        failures.append(f"missing expected text {required_text!r}")
    missing_spans = [span for span in required_spans if span not in spans]
    if missing_spans:
        failures.append(f"missing spans {','.join(missing_spans)}")
    if _contains_sensitive(replay):
        failures.append("sensitive replay payload")

    trajectory_pass = not missing_spans and not runtime_failures
    score = 1.0 if not failures else 0.0
    return {
        "case_id": case.get("case_id"),
        "score": round(score, 4),
        "passed": not failures,
        "critical": metadata.get("critical") is True,
        "trajectory_pass": trajectory_pass,
        "failures": failures,
    }


def evaluate_cases(
    cases: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    case_results = [
        evaluate_case(case, observations.get(str(case.get("case_id")), {}))
        if observations is not None
        else evaluate_case(case)
        for case in cases
    ]
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
    overall_score_value = metrics.get("overall_score")
    if overall_score_value is None:
        overall_score_value = metrics.get("average_score")
    overall_score = float(overall_score_value or 0.0)
    trajectory_pass_rate = float(metrics.get("trajectory_pass_rate") or 0.0)
    critical_pass_rate_value = metrics.get("critical_pass_rate")
    if critical_pass_rate_value is None:
        critical_pass_rate_value = metrics.get("pass_rate")
    critical_pass_rate = float(critical_pass_rate_value or 0.0)

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
        baseline_score_value = baseline_metrics.get("overall_score")
        if baseline_score_value is None:
            baseline_score_value = baseline_metrics.get("average_score")
        baseline_score = float(baseline_score_value or 0.0)
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
        f"Evidence scope: `{result.get('evidence_scope', 'unknown')}`",
        f"Joined observations: `{result.get('observations', {}).get('joined_count', 0)}`",
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
