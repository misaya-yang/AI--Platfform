from __future__ import annotations

from pathlib import Path

import pytest

from scripts.harness import codex_runtime_write_gate as write_gate


def test_fork_path_requires_controlled_source_checkout(tmp_path: Path) -> None:
    with pytest.raises(write_gate.GateError, match="controlled Codex fork"):
        write_gate._fork_path(str(tmp_path))


def test_gate_runs_only_the_lifecycle_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("", encoding="utf-8")
    (tmp_path / "codex-rs").mkdir()
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(write_gate.subprocess, "run", fake_run)
    assert write_gate.run_gate(tmp_path) == 0
    assert captured["command"] == [
        "just",
        "test",
        "-p",
        "ai-platform-agent-runtime",
    ]
    assert captured["cwd"] == tmp_path
