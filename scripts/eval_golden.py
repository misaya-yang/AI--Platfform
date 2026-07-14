#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from src.services.eval.golden import (
    apply_gate,
    evaluate_cases,
    load_jsonl,
    load_observations,
    summarize_cases,
    validate_cases,
    validate_observations,
    write_gate_report,
)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _redact_runtime_secrets(value: str) -> str:
    for name in ("GATEWAY_TOKEN", "GATEWAY_ADMIN_JWT", "GATEWAY_API_KEY"):
        secret = os.environ.get(name)
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


def _eval_api_url(base_url: str, path: str) -> str:
    root = base_url.rstrip("/")
    parsed = urlsplit(root)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--base-url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("--base-url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("--base-url must not contain a query or fragment")
    if root.endswith("/api/v1/eval"):
        return f"{root}{path}"
    if root.endswith("/api/v1"):
        return f"{root}/eval{path}"
    return f"{root}/api/v1/eval{path}"


def _request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("GATEWAY_TOKEN") or os.environ.get("GATEWAY_ADMIN_JWT")
    api_key = os.environ.get("GATEWAY_API_KEY")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["X-API-Key"] = api_key
    if not token and not api_key:
        raise ValueError("GATEWAY_TOKEN/GATEWAY_ADMIN_JWT or GATEWAY_API_KEY is required")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        _eval_api_url(base_url, path),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is operator supplied
            result = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Eval API {method} {path} failed with HTTP {exc.code}") from exc
    except URLError as exc:
        reason = _redact_runtime_secrets(str(exc.reason))
        raise RuntimeError(f"Eval API {method} {path} failed: {reason}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Eval API {method} {path} returned a non-object response")
    return result


def _run_id_from_response(payload: dict[str, Any]) -> str:
    run_ids = {str(payload["run_id"])} if payload.get("run_id") else set()
    for job in payload.get("jobs") or []:
        if isinstance(job, dict) and job.get("run_id"):
            run_ids.add(str(job["run_id"]))
    if len(run_ids) != 1:
        raise RuntimeError("Live experiment must create exactly one suite run")
    return run_ids.pop()


def _is_compatible(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("compatible"), bool):
        return bool(value["compatible"])
    if isinstance(value.get("is_compatible"), bool):
        return bool(value["is_compatible"])
    return str(value.get("status") or "").lower() in {"compatible", "pass"}


def _numeric_values(payload: Any) -> dict[str, int | float | None]:
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if value is None or (isinstance(value, int | float) and not isinstance(value, bool))
    }


def _live_result(
    *,
    experiment_id: str,
    baseline_run_id: str,
    candidate_run_id: str,
    run_status: str,
    comparison: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "eval-live-regression-v1",
        "experiment_id": experiment_id,
        "baseline_run_id": baseline_run_id,
        "candidate_run_id": candidate_run_id,
        "run_status": run_status,
    }
    if error:
        result["error"] = _redact_runtime_secrets(error)
    if comparison is None:
        return result
    compatibility = comparison.get("compatibility")
    gate = comparison.get("gate")
    result["compatible"] = _is_compatible(compatibility)
    if isinstance(compatibility, dict):
        result["compatibility_status"] = compatibility.get("status")
    if isinstance(gate, dict):
        result["gate"] = {
            "status": gate.get("status"),
            "failures": [_redact_runtime_secrets(str(item)) for item in gate.get("failures") or []],
        }
    result["candidate_metrics"] = _numeric_values(
        comparison.get("candidate_summary") or comparison.get("metrics")
    )
    result["deltas"] = _numeric_values(comparison.get("deltas"))
    return result


def _load_valid_cases(path: str) -> list[dict[str, Any]] | None:
    cases = load_jsonl(path)
    validation = validate_cases(cases)
    if not validation["valid"]:
        _print_json(validation)
        return None
    return cases


def cmd_validate(args: argparse.Namespace) -> int:
    cases = load_jsonl(args.path)
    result = validate_cases(cases)
    _print_json(result)
    return 0 if result["valid"] else 1


def cmd_summarize(args: argparse.Namespace) -> int:
    cases = _load_valid_cases(args.path)
    if cases is None:
        return 1
    _print_json(summarize_cases(cases))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    cases = _load_valid_cases(args.path)
    if cases is None:
        return 1
    observations = None
    observation_summary: dict[str, Any] = {
        "source": None,
        "case_count": len(cases),
        "observation_count": 0,
        "joined_count": 0,
        "errors": [],
    }
    evidence_scope = "legacy_inline_replay"
    if args.observations:
        observations = load_observations(args.observations)
        validation = validate_observations(cases, observations)
        observation_summary = {"source": str(Path(args.observations)), **validation}
        if not validation["valid"]:
            _print_json(observation_summary)
            return 1
        evidence_scope = "recorded_offline_observation"

    baseline_metrics = None
    baseline = None
    if args.baseline_report:
        baseline_payload = json.loads(Path(args.baseline_report).read_text(encoding="utf-8"))
        if not isinstance(baseline_payload, dict):
            raise ValueError("Baseline report must be a JSON object")
        baseline_metrics = baseline_payload.get("metrics")
        if not isinstance(baseline_metrics, dict):
            baseline_gate = baseline_payload.get("gate")
            baseline_metrics = (
                baseline_gate.get("metrics") if isinstance(baseline_gate, dict) else None
            )
        if not isinstance(baseline_metrics, dict):
            raise ValueError("Baseline report must contain metrics")
        baseline = {"source": str(Path(args.baseline_report)), "metrics": baseline_metrics}

    metrics = evaluate_cases(cases, observations)
    gate = apply_gate(metrics, baseline_metrics=baseline_metrics)
    result = {
        "schema_version": "eval-regression-gate-v1",
        "source": str(Path(args.path)),
        "evidence_scope": evidence_scope,
        "observations": observation_summary,
        "summary": summarize_cases(cases),
        "metrics": metrics,
        "gate": gate,
    }
    if baseline is not None:
        result["baseline"] = baseline
    write_gate_report(result, args.output, args.markdown)
    _print_json(result)
    return 0 if gate["status"] == "pass" else 1


def cmd_import(args: argparse.Namespace) -> int:
    cases = _load_valid_cases(args.path)
    if cases is None:
        return 1
    _print_json(
        {
            "status": "ready",
            "mode": "api-required",
            "message": "Use POST /api/v1/eval/datasets/{dataset_id}/examples:import with this JSONL payload.",
            "case_count": len(cases),
        }
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    _print_json(
        {
            "status": "ready",
            "mode": "api-required",
            "message": "Use GET /api/v1/eval/datasets/{dataset_id}/examples:export and write the response examples as JSONL.",
            "dataset_id": args.dataset_id,
        }
    )
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    if not 1 <= args.repetitions <= 10:
        raise ValueError("--repetitions must be between 1 and 10")
    if args.poll_interval < 0 or args.timeout <= 0 or args.request_timeout <= 0:
        raise ValueError("poll interval must be non-negative and timeouts must be positive")

    experiment_path = f"/experiments/{quote(args.experiment_id, safe='')}"
    experiment = _request_json(
        args.base_url,
        "GET",
        experiment_path,
        timeout=args.request_timeout,
    )
    baseline_run_id = str(args.baseline_run_id or experiment.get("baseline_run_id") or "")
    if not baseline_run_id:
        raise ValueError("No baseline is configured; pass --baseline-run-id or promote one first")

    candidate_config: dict[str, Any] = {}
    if args.system_prompt_file:
        candidate_config["system_prompt_override"] = Path(args.system_prompt_file).read_text(
            encoding="utf-8"
        )
    body: dict[str, Any] = {
        "run_mode": "live_candidate",
        "evaluator_ids": args.evaluator_id,
        "repetitions": args.repetitions,
        "baseline_run_id": baseline_run_id,
        "metadata": {"source": "eval_golden_live_cli"},
    }
    if args.dataset_id:
        body["dataset_id"] = args.dataset_id
    if candidate_config:
        body["candidate_config"] = candidate_config

    started = _request_json(
        args.base_url,
        "POST",
        f"{experiment_path}:run",
        payload=body,
        timeout=args.request_timeout,
    )
    candidate_run_id = _run_id_from_response(started)
    run_path = f"/experiment-runs/{quote(candidate_run_id, safe='')}"
    deadline = time.monotonic() + args.timeout
    while True:
        run = _request_json(
            args.base_url,
            "GET",
            run_path,
            timeout=args.request_timeout,
        )
        observed_mode = run.get("run_mode") or (run.get("target_snapshot") or {}).get("run_mode")
        if observed_mode != "live_candidate":
            result = _live_result(
                experiment_id=args.experiment_id,
                baseline_run_id=baseline_run_id,
                candidate_run_id=candidate_run_id,
                run_status=str(run.get("status") or "unknown"),
                error="Server did not confirm live_candidate mode",
            )
            _print_json(result)
            return 1
        status = str(run.get("status") or "unknown")
        if status == "succeeded":
            break
        if status in {"failed", "cancelled"}:
            result = _live_result(
                experiment_id=args.experiment_id,
                baseline_run_id=baseline_run_id,
                candidate_run_id=candidate_run_id,
                run_status=status,
                error=str(run.get("error_message") or f"Live run {status}"),
            )
            _print_json(result)
            return 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result = _live_result(
                experiment_id=args.experiment_id,
                baseline_run_id=baseline_run_id,
                candidate_run_id=candidate_run_id,
                run_status=status,
                error=f"Timed out after {args.timeout:g}s",
            )
            _print_json(result)
            return 1
        if args.poll_interval:
            time.sleep(min(args.poll_interval, remaining))

    query = urlencode(
        {
            "baseline_run_id": baseline_run_id,
            "candidate_run_id": candidate_run_id,
        }
    )
    comparison = _request_json(
        args.base_url,
        "GET",
        f"/experiment-runs:compare?{query}",
        timeout=args.request_timeout,
    )
    result = _live_result(
        experiment_id=args.experiment_id,
        baseline_run_id=baseline_run_id,
        candidate_run_id=candidate_run_id,
        run_status="succeeded",
        comparison=comparison,
    )
    _print_json(result)
    gate = comparison.get("gate")
    gate_status = str(gate.get("status") or "") if isinstance(gate, dict) else ""
    if not _is_compatible(comparison.get("compatibility")):
        return 1
    if gate_status not in {"pass", "warning"}:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and run Eval golden sets.")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("path")
    validate.set_defaults(func=cmd_validate)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("path")
    summarize.set_defaults(func=cmd_summarize)

    gate = sub.add_parser("gate")
    gate.add_argument("path")
    gate.add_argument("--observations")
    gate.add_argument("--baseline-report")
    gate.add_argument("--output", default="reports/eval-regression/latest.json")
    gate.add_argument("--markdown", default="reports/eval-regression/latest.md")
    gate.set_defaults(func=cmd_gate)

    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("path")
    import_cmd.set_defaults(func=cmd_import)

    export_cmd = sub.add_parser("export")
    export_cmd.add_argument("dataset_id")
    export_cmd.set_defaults(func=cmd_export)

    live = sub.add_parser(
        "live",
        help="Run the current Agent and gate it against an approved baseline.",
    )
    live.add_argument("experiment_id")
    live.add_argument("--evaluator-id", action="append", required=True)
    live.add_argument("--dataset-id")
    live.add_argument("--baseline-run-id")
    live.add_argument("--repetitions", type=int, default=3)
    live.add_argument("--system-prompt-file")
    live.add_argument(
        "--base-url",
        default=os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8080"),
    )
    live.add_argument("--poll-interval", type=float, default=2.0)
    live.add_argument("--timeout", type=float, default=1800.0)
    live.add_argument("--request-timeout", type=float, default=30.0)
    live.set_defaults(func=cmd_live)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - CLI should report clear failure text
        print(f"eval_golden failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
