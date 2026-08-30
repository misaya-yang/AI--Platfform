#!/usr/bin/env python3
"""Execute ARC-08 integration/fresh/rollback gates without placeholder passes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = ROOT / "deploy/release/integration-gates.json"
SCHEMA = "ai-platform/integration-gates/v1"
SKIP = re.compile(r"\b(?:SKIP(?:PED)?|NOT APPLICABLE)\b", re.I)


class IntegrationGateError(RuntimeError):
    pass


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IntegrationGateError(f"{label} unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrationGateError(f"{label} must be an object")
    return value


def load_spec(path: Path) -> dict[str, Any]:
    spec = _load(path, "integration gate manifest")
    if spec.get("schema_version") != SCHEMA or not isinstance(spec.get("gates"), dict):
        raise IntegrationGateError("unsupported integration gate manifest")
    gates = spec["gates"]
    for gate_id, gate in gates.items():
        if not isinstance(gate, dict) or gate.get("tier") not in {"L2", "L3"}:
            raise IntegrationGateError(f"invalid integration gate: {gate_id}")
        if not isinstance(gate.get("required_env"), list) or not isinstance(gate.get("steps"), list):
            raise IntegrationGateError(f"gate requires required_env/steps lists: {gate_id}")
        ids: set[str] = set()
        for step in gate["steps"]:
            if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                raise IntegrationGateError(f"invalid gate step: {gate_id}")
            if step["id"] in ids or bool(step.get("command")) == bool(step.get("gate")):
                raise IntegrationGateError(f"duplicate/ambiguous gate step: {gate_id}:{step['id']}")
            ids.add(step["id"])
            if "gate" in step and step["gate"] not in gates:
                raise IntegrationGateError(f"unknown nested gate: {step['gate']}")
            if "command" in step and (
                not isinstance(step["command"], list)
                or not step["command"]
                or not all(isinstance(value, str) and value for value in step["command"])
            ):
                raise IntegrationGateError(f"invalid command: {gate_id}:{step['id']}")
    _check_cycles(gates)
    return spec


def _check_cycles(gates: dict[str, Any]) -> None:
    def visit(gate_id: str, stack: tuple[str, ...]) -> None:
        if gate_id in stack:
            raise IntegrationGateError(f"integration gate cycle: {' -> '.join((*stack, gate_id))}")
        for step in gates[gate_id]["steps"]:
            if "gate" in step:
                visit(step["gate"], (*stack, gate_id))

    for gate_id in gates:
        visit(gate_id, ())


def _source_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _release_identity(root: Path) -> tuple[str | None, str | None]:
    raw = os.environ.get("AI_PLATFORM_COMPATIBILITY_MANIFEST_PATH")
    if not raw:
        return None, None
    manifest = _load(Path(raw), "runtime compatibility manifest")
    if manifest.get("status") not in {"release_candidate", "released"}:
        raise IntegrationGateError("runtime compatibility manifest is not candidate/released")
    release_id = manifest.get("release_id")
    source = manifest.get("source")
    if not isinstance(release_id, str) or not isinstance(source, dict):
        raise IntegrationGateError("runtime compatibility manifest identity is incomplete")
    source_sha = source.get("git_sha")
    if not isinstance(source_sha, str):
        raise IntegrationGateError("runtime compatibility source identity is incomplete")
    current_sha = _source_sha(root)
    if current_sha != source_sha:
        raise IntegrationGateError(
            "current checkout does not match runtime compatibility source Git SHA"
        )
    dirty = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if dirty.returncode != 0 or dirty.stdout:
        raise IntegrationGateError("release integration requires a clean source checkout")
    return release_id, source_sha


def _missing_env(names: list[str]) -> list[str]:
    missing: list[str] = []
    for name in names:
        value = os.environ.get(name, "")
        if not value or (name.endswith("_AUTHORIZED") and value != "1"):
            missing.append(name)
    return missing


def _write(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_gate(
    gate_id: str,
    spec: dict[str, Any],
    *,
    root: Path,
    dry_run: bool,
    receipt_path: Path,
) -> int:
    gate = spec["gates"].get(gate_id)
    if not isinstance(gate, dict):
        raise IntegrationGateError(f"unknown integration gate: {gate_id}")
    release_id, manifest_source_sha = _release_identity(root)
    receipt: dict[str, Any] = {
        "schema_version": "ai-platform/integration-gate-receipt/v1",
        "gate": gate_id,
        "tier": gate["tier"],
        "release_id": release_id,
        "source_git_sha": manifest_source_sha or _source_sha(root),
        "result": "blocked",
        "unexpected_skips": 0,
        "steps": [],
    }
    missing = _missing_env(gate["required_env"])
    if missing:
        receipt["blockers"] = [f"missing required environment name: {name}" for name in missing]
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 2
    if dry_run:
        receipt["result"] = "dry-run"
        receipt["steps"] = [
            {"id": step["id"], "command": step.get("command"), "gate": step.get("gate")}
            for step in gate["steps"]
        ]
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    started = time.monotonic()
    for step in gate["steps"]:
        step_started = time.monotonic()
        if "gate" in step:
            nested_path = receipt_path.with_name(f"{receipt_path.stem}-{step['gate']}.json")
            exit_code = run_gate(
                step["gate"], spec, root=root, dry_run=False, receipt_path=nested_path
            )
            output = nested_path.read_text(encoding="utf-8") if nested_path.is_file() else ""
            command = ["integration-gate", step["gate"]]
        else:
            command = step["command"]
            process = subprocess.run(
                command,
                cwd=root,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                check=False,
            )
            exit_code = process.returncode
            output = f"{process.stdout}\n{process.stderr}".strip()
            if output:
                print(output)
        skips = len(SKIP.findall(output))
        record = {
            "id": step["id"],
            "command": command,
            "exit_code": exit_code,
            "duration_seconds": round(time.monotonic() - step_started, 3),
            "skip_markers": skips,
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        }
        receipt["steps"].append(record)
        receipt["unexpected_skips"] += skips
        if exit_code != 0 or skips:
            receipt["result"] = "fail" if exit_code == 1 else "blocked"
            receipt["duration_seconds"] = round(time.monotonic() - started, 3)
            _write(receipt_path, receipt)
            return 1 if exit_code == 1 or skips else 2
    receipt["result"] = "pass"
    receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    _write(receipt_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args(argv)
    receipt = args.receipt or ROOT / f"tmp/gate-evidence/{args.gate}-integration.json"
    try:
        return run_gate(
            args.gate,
            load_spec(args.spec),
            root=args.repo_root.resolve(),
            dry_run=args.dry_run,
            receipt_path=receipt,
        )
    except IntegrationGateError as exc:
        print(f"INTEGRATION GATE ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
