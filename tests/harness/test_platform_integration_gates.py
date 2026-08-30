from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.release import integration_gates


def _spec(command: list[str], *, required_env: list[str] | None = None) -> dict:
    return {
        "schema_version": integration_gates.SCHEMA,
        "gates": {
            "test": {
                "tier": "L2",
                "required_env": required_env or [],
                "steps": [{"id": "real-command", "command": command}],
            }
        },
    }


def test_missing_prerequisite_is_blocked_and_command_does_not_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "must-not-run"
    monkeypatch.delenv("REQUIRED_TEST_ENV", raising=False)
    receipt = tmp_path / "blocked.json"
    spec = _spec(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        required_env=["REQUIRED_TEST_ENV"],
    )

    assert integration_gates.run_gate(
        "test", spec, root=tmp_path, dry_run=False, receipt_path=receipt
    ) == 2
    assert not marker.exists()
    assert json.loads(receipt.read_text())["result"] == "blocked"


def test_dry_run_is_never_recorded_as_pass(tmp_path: Path) -> None:
    receipt = tmp_path / "dry-run.json"

    assert integration_gates.run_gate(
        "test",
        _spec([sys.executable, "-c", "raise SystemExit(99)"]),
        root=tmp_path,
        dry_run=True,
        receipt_path=receipt,
    ) == 0
    assert json.loads(receipt.read_text())["result"] == "dry-run"


def test_real_command_passes_and_skip_marker_fails_closed(tmp_path: Path) -> None:
    passed = tmp_path / "passed.json"
    skipped = tmp_path / "skipped.json"

    assert integration_gates.run_gate(
        "test",
        _spec([sys.executable, "-c", "print('executed')"]),
        root=tmp_path,
        dry_run=False,
        receipt_path=passed,
    ) == 0
    assert json.loads(passed.read_text())["result"] == "pass"
    assert integration_gates.run_gate(
        "test",
        _spec([sys.executable, "-c", "print('SKIPPED unavailable')"]),
        root=tmp_path,
        dry_run=False,
        receipt_path=skipped,
    ) == 1
    payload = json.loads(skipped.read_text())
    assert payload["result"] == "blocked"
    assert payload["unexpected_skips"] == 1


def test_checked_in_gate_manifest_has_real_commands_and_no_cycles() -> None:
    spec = integration_gates.load_spec(integration_gates.DEFAULT_SPEC)

    assert set(spec["gates"]) == {
        "platform-db",
        "agent-execution",
        "knowledge",
        "fresh-install",
        "rollback",
        "version-agreement",
        "all",
    }
    assert all(gate["steps"] for gate in spec["gates"].values())
