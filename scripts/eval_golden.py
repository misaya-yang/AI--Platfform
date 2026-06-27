#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.services.eval.golden import (
    apply_gate,
    evaluate_cases,
    load_jsonl,
    summarize_cases,
    validate_cases,
    write_gate_report,
)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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
    metrics = evaluate_cases(cases)
    gate = apply_gate(metrics)
    result = {
        "schema_version": "eval-regression-gate-v1",
        "source": str(Path(args.path)),
        "summary": summarize_cases(cases),
        "metrics": metrics,
        "gate": gate,
    }
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and run offline Eval golden sets.")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("path")
    validate.set_defaults(func=cmd_validate)

    summarize = sub.add_parser("summarize")
    summarize.add_argument("path")
    summarize.set_defaults(func=cmd_summarize)

    gate = sub.add_parser("gate")
    gate.add_argument("path")
    gate.add_argument("--output", default="reports/eval-regression/latest.json")
    gate.add_argument("--markdown", default="reports/eval-regression/latest.md")
    gate.set_defaults(func=cmd_gate)

    import_cmd = sub.add_parser("import")
    import_cmd.add_argument("path")
    import_cmd.set_defaults(func=cmd_import)

    export_cmd = sub.add_parser("export")
    export_cmd.add_argument("dataset_id")
    export_cmd.set_defaults(func=cmd_export)

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
