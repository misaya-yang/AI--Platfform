from __future__ import annotations

import json
import math
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
    "output_not_contains",
    "required_span_kind",
    "no_sensitive_output",
    "latency_ms_lt",
    "total_tokens_lt",
    "cost_cents_lt",
    "tool_called",
    "tool_not_called",
    "failure_mode_absent",
}

SUPPORTED_TOOL_EXPECTATION_FIELDS = {
    "name",
    "required",
    "forbidden",
    "arguments_subset",
    "order",
    "max_calls",
    "status",
}

STRING_ASSERTIONS = {
    "output_contains",
    "output_not_contains",
    "required_span_kind",
    "tool_called",
    "tool_not_called",
    "failure_mode_absent",
}

NUMERIC_ASSERTIONS = {"latency_ms_lt", "total_tokens_lt", "cost_cents_lt"}


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
    for key in (
        "case_id",
        "input",
        "expected_output",
        "expected_trajectory",
        "assertions",
        "metadata",
    ):
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
    if isinstance(expected_trajectory, dict) and "required_span_kinds" in expected_trajectory:
        required_spans = expected_trajectory.get("required_span_kinds")
        if not isinstance(required_spans, list) or any(
            not isinstance(item, str) or not item.strip() for item in required_spans
        ):
            errors.append("expected_trajectory.required_span_kinds must be a string list")
    expected_output = case.get("expected_output")
    if isinstance(expected_output, dict):
        for key in ("reference", "rubric"):
            if key in expected_output and (
                not isinstance(expected_output[key], str) or not expected_output[key].strip()
            ):
                errors.append(f"expected_output.{key} must be a non-empty string")
        for key in ("contains", "not_contains"):
            if key not in expected_output:
                continue
            value = expected_output[key]
            if isinstance(value, str):
                valid = bool(value.strip())
            else:
                valid = (
                    isinstance(value, list)
                    and bool(value)
                    and all(isinstance(item, str) and item.strip() for item in value)
                )
            if not valid:
                errors.append(f"expected_output.{key} must be a non-empty string or string list")
    if isinstance(expected_trajectory, dict) and "tools" in expected_trajectory:
        tools = expected_trajectory.get("tools")
        if not isinstance(tools, list):
            errors.append("expected_trajectory.tools must be a list")
        else:
            for index, tool in enumerate(tools, start=1):
                prefix = f"expected_trajectory.tools[{index}]"
                if not isinstance(tool, dict):
                    errors.append(f"{prefix} must be an object")
                    continue
                unknown = sorted(set(tool) - SUPPORTED_TOOL_EXPECTATION_FIELDS)
                if unknown:
                    errors.append(f"{prefix} has unsupported fields {', '.join(unknown)}")
                if not isinstance(tool.get("name"), str) or not tool.get("name", "").strip():
                    errors.append(f"{prefix}.name must be a non-empty string")
                for key in ("required", "forbidden"):
                    if key in tool and not isinstance(tool[key], bool):
                        errors.append(f"{prefix}.{key} must be boolean")
                if tool.get("required") is True and tool.get("forbidden") is True:
                    errors.append(f"{prefix} cannot be both required and forbidden")
                if "arguments_subset" in tool and not isinstance(tool["arguments_subset"], dict):
                    errors.append(f"{prefix}.arguments_subset must be an object")
                if "order" in tool and (
                    isinstance(tool["order"], bool)
                    or not isinstance(tool["order"], int)
                    or tool["order"] < 1
                ):
                    errors.append(f"{prefix}.order must be a positive integer")
                if "max_calls" in tool and (
                    isinstance(tool["max_calls"], bool)
                    or not isinstance(tool["max_calls"], int)
                    or tool["max_calls"] < 0
                ):
                    errors.append(f"{prefix}.max_calls must be a non-negative integer")
                if "status" in tool and (
                    not isinstance(tool["status"], str) or not tool["status"].strip()
                ):
                    errors.append(f"{prefix}.status must be a non-empty string")
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
            elif assertion_type in STRING_ASSERTIONS and (
                not isinstance(assertion.get("value"), str) or not assertion["value"].strip()
            ):
                errors.append(f"assertions[{index}].value must be a non-empty string")
            elif assertion_type in NUMERIC_ASSERTIONS and (
                isinstance(assertion.get("value"), bool)
                or not isinstance(assertion.get("value"), (int, float))
                or not math.isfinite(float(assertion["value"]))
                or assertion["value"] <= 0
            ):
                errors.append(f"assertions[{index}].value must be a positive number")
    split = case.get("split", "regression")
    if not isinstance(split, str) or not split:
        errors.append("split must be a non-empty string")
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    if metadata.get("critical") is not None and not isinstance(metadata.get("critical"), bool):
        errors.append("metadata.critical must be boolean when present")
    if metadata.get("behavior_confirmed") is not None and not isinstance(
        metadata.get("behavior_confirmed"), bool
    ):
        errors.append("metadata.behavior_confirmed must be boolean when present")
    if metadata.get("owner") is not None and not isinstance(metadata.get("owner"), str):
        errors.append("metadata.owner must be a string when present")
    if metadata.get("difficulty") is not None and not isinstance(
        metadata.get("difficulty"), str
    ):
        errors.append("metadata.difficulty must be a string when present")
    if metadata.get("review_status") is not None and metadata.get("review_status") not in {
        "pending",
        "approved",
        "rejected",
        "needs_fix",
    }:
        errors.append("metadata.review_status is invalid")
    if metadata.get("tags") is not None and (
        not isinstance(metadata.get("tags"), list)
        or any(not isinstance(tag, str) or not tag.strip() for tag in metadata["tags"])
    ):
        errors.append("metadata.tags must be a string list when present")
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
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if not isinstance(value, str)
        else value
    )
    return redact_trace_text(text) != text


def _case_replay(case: dict[str, Any]) -> dict[str, Any]:
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    replay = metadata.get("replay") if isinstance(metadata.get("replay"), dict) else {}
    return replay


def _expected_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _tool_call_name(call: dict[str, Any]) -> str:
    name = str(call.get("name") or call.get("tool_name") or "").strip()
    return name.removeprefix("tool:")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any] | None:
    value = call.get("arguments", call.get("args", call.get("input_preview")))
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _observed_tool_calls(replay: dict[str, Any]) -> list[Any] | None:
    if "tool_calls" in replay:
        calls = replay.get("tool_calls")
        return calls if isinstance(calls, list) else None
    if "spans" not in replay:
        return None
    spans = replay.get("spans")
    if not isinstance(spans, list):
        return None
    return [
        span
        for span in spans
        if isinstance(span, dict) and span.get("span_kind") == "tool_execution"
    ]


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _is_subset(value, actual[key]) for key, value in expected.items()
        )
    return expected == actual


def _evaluate_tool_expectations(expected_tools: list[Any], replay: dict[str, Any]) -> list[str]:
    if not expected_tools:
        return []
    calls = _observed_tool_calls(replay)
    if calls is None:
        return ["expected tools require tool_calls or spans evidence"]
    if any(not isinstance(call, dict) for call in calls):
        return ["tool_calls evidence contains a non-object entry"]

    failures: list[str] = []
    typed_calls = [call for call in calls if isinstance(call, dict)]
    for expected in expected_tools:
        if not isinstance(expected, dict):
            failures.append("invalid tool expectation")
            continue
        name = str(expected.get("name") or "").strip()
        matches = [
            (index, call)
            for index, call in enumerate(typed_calls, start=1)
            if _tool_call_name(call) == name
        ]
        if expected.get("forbidden") is True:
            if matches:
                failures.append(f"forbidden tool called {name!r}")
            continue
        if "max_calls" in expected and len(matches) > int(expected["max_calls"]):
            failures.append(f"tool {name!r} called {len(matches)} > {expected['max_calls']}")
        required = expected.get("required", True) is True
        if not matches:
            if required:
                failures.append(f"required tool not called {name!r}")
            continue

        candidates = matches
        if "order" in expected:
            candidates = [item for item in candidates if item[0] == expected["order"]]
        if "status" in expected:
            candidates = [
                item
                for item in candidates
                if str(item[1].get("status") or "") == expected["status"]
            ]
        if "arguments_subset" in expected:
            candidates = [
                item
                for item in candidates
                if _is_subset(expected["arguments_subset"], _tool_call_arguments(item[1]))
            ]
        if not candidates and (required or matches):
            failures.append(f"tool {name!r} did not match expected order, status, or arguments")
    return failures


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
        elif assertion_type == "output_not_contains":
            needle = str(value or "").strip()
            if not needle or needle.lower() in output.lower():
                failures.append(f"output_not_contains found {needle!r}")
        elif assertion_type == "required_span_kind":
            span_kind = str(value or "").strip()
            if not span_kind or span_kind not in spans:
                failures.append(f"required_span_kind missing {span_kind!r}")
        elif assertion_type == "no_sensitive_output":
            if _contains_sensitive(replay):
                failures.append("no_sensitive_output detected sensitive replay payload")
        elif assertion_type in NUMERIC_ASSERTIONS:
            evidence_key = {
                "latency_ms_lt": "total_latency_ms",
                "total_tokens_lt": "total_tokens",
                "cost_cents_lt": "total_cost_cents",
            }[assertion_type]
            try:
                limit = float(value)
                actual = float(replay.get(evidence_key))
                if not math.isfinite(actual) or actual < 0:
                    raise ValueError
            except (TypeError, ValueError):
                failures.append(f"{assertion_type} requires numeric evidence")
            else:
                if actual >= limit:
                    failures.append(f"{assertion_type} {actual:g} >= {limit:g}")
        elif assertion_type in {"tool_called", "tool_not_called"}:
            tool_name = str(value or "").strip()
            calls = _observed_tool_calls(replay)
            if calls is None or any(not isinstance(call, dict) for call in calls):
                failures.append(f"{assertion_type} requires valid tool_calls or spans evidence")
            else:
                observed_names = {_tool_call_name(call) for call in calls if isinstance(call, dict)}
                called = tool_name in observed_names
                if assertion_type == "tool_called" and not called:
                    failures.append(f"tool_called missing {tool_name!r}")
                elif assertion_type == "tool_not_called" and called:
                    failures.append(f"tool_not_called observed {tool_name!r}")
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
    validation_errors = validate_case(case)
    if validation_errors:
        return _failed_case_result(
            case, f"invalid behavior contract: {'; '.join(validation_errors)}"
        )
    replay = observation if isinstance(observation, dict) else _case_replay(case)
    if not replay:
        return _failed_case_result(case, "missing replay observation")
    output = str(replay.get("output_preview") or "")
    spans = replay.get("span_kinds") if isinstance(replay.get("span_kinds"), list) else []
    status = str(replay.get("status") or "")
    expected_output = (
        case.get("expected_output") if isinstance(case.get("expected_output"), dict) else {}
    )
    expected_trajectory = (
        case.get("expected_trajectory") if isinstance(case.get("expected_trajectory"), dict) else {}
    )
    metadata = case.get("metadata") if isinstance(case.get("metadata"), dict) else {}
    required_texts = _expected_strings(expected_output.get("contains"))
    if not required_texts:
        required_texts = _expected_strings(expected_output.get("output_preview"))
    forbidden_texts = _expected_strings(expected_output.get("not_contains"))
    required_spans = [
        str(item)
        for item in expected_trajectory.get("required_span_kinds", [])
        if isinstance(item, str) and item
    ]

    assertions = case.get("assertions") if isinstance(case.get("assertions"), list) else []
    failures = _evaluate_assertions(assertions, replay)
    runtime_failures = _evaluate_runtime_expectations(case, replay)
    tool_failures = _evaluate_tool_expectations(expected_trajectory.get("tools") or [], replay)
    failures.extend(runtime_failures)
    failures.extend(tool_failures)
    if not status:
        failures.append("missing replay status")
    elif status != "succeeded":
        failures.append(f"status={status}")
    for required_text in required_texts:
        if required_text.lower() not in output.lower():
            failures.append(f"missing expected text {required_text!r}")
    for forbidden_text in forbidden_texts:
        if forbidden_text.lower() in output.lower():
            failures.append(f"found forbidden text {forbidden_text!r}")
    missing_spans = [span for span in required_spans if span not in spans]
    if missing_spans:
        failures.append(f"missing spans {','.join(missing_spans)}")
    if _contains_sensitive(replay):
        failures.append("sensitive replay payload")

    trajectory_assertions = [
        assertion
        for assertion in assertions
        if isinstance(assertion, dict)
        and assertion.get("type") in {"required_span_kind", "tool_called", "tool_not_called"}
    ]
    trajectory_pass = (
        not missing_spans
        and not runtime_failures
        and not tool_failures
        and not _evaluate_assertions(trajectory_assertions, replay)
    )
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
        failures.append(
            f"overall_score {overall_score:.4f} < {gate_thresholds['overall_score']:.4f}"
        )
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
            failures.append(
                f"candidate score {overall_score:.4f} < baseline tolerance {allowed:.4f}"
            )

    return {
        "status": "fail" if failures else "pass",
        "thresholds": gate_thresholds,
        "metrics": metrics,
        "failures": failures,
    }


def write_gate_report(
    result: dict[str, Any], json_path: str | Path, markdown_path: str | Path
) -> None:
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
