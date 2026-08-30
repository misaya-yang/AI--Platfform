"""Git-provenance tests for the LOC no-growth baseline."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.harness import loc_no_growth_gate as gate


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "loc-test@example.invalid")
    _git(root, "config", "user.name", "LOC Test")
    (root / "src").mkdir()
    (root / "web" / "src").mkdir(parents=True)
    (root / "src" / "big.py").write_text("line\n" * 801, encoding="utf-8")
    (root / "web" / "src" / "big.ts").write_text("line\n" * 501, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "baseline source")
    return root, _git(root, "rev-parse", "HEAD")


def _baseline(base_sha: str, *, python_lines: int = 801) -> dict:
    return {
        "schema": "ai-gateway/baseline/loc-baseline/v1",
        "baseline_id": "test",
        "base_git_sha": base_sha,
        "thresholds": {
            "python_new_file_max": gate.PY_THRESHOLD,
            "typescript_new_file_max": gate.TS_THRESHOLD,
        },
        "oversized_python": {
            "files": [{"file": "src/big.py", "lines": python_lines}]
        },
        "oversized_typescript": {
            "files": [{"file": "web/src/big.ts", "lines": 501}]
        },
    }


def test_provenance_checks_every_recorded_oversized_blob(tmp_path: Path) -> None:
    root, base_sha = _repo(tmp_path)

    result = gate.verify_baseline_provenance(root, _baseline(base_sha))

    assert result == {
        "result": "pass",
        "base_git_sha": base_sha,
        "constrained_files_checked": 2,
    }


def test_same_change_cannot_raise_the_recorded_loc_to_self_bless(tmp_path: Path) -> None:
    root, base_sha = _repo(tmp_path)
    (root / "src" / "big.py").write_text("line\n" * 805, encoding="utf-8")
    _git(root, "add", "src/big.py")
    _git(root, "commit", "-q", "-m", "grow source")

    with pytest.raises(gate.LocBaselineError, match=r"recorded 805, Git object 801"):
        gate.verify_baseline_provenance(root, _baseline(base_sha, python_lines=805))


@pytest.mark.parametrize("base_sha", ["not-a-sha", "0" * 40])
def test_unparseable_or_missing_base_sha_fails_closed(
    tmp_path: Path,
    base_sha: str,
) -> None:
    root, _real_sha = _repo(tmp_path)

    with pytest.raises(gate.LocBaselineError):
        gate.verify_baseline_provenance(root, _baseline(base_sha))


def test_non_ancestor_base_sha_fails_closed(tmp_path: Path) -> None:
    root, common_parent = _repo(tmp_path)
    (root / "src" / "branch.py").write_text("main\n", encoding="utf-8")
    _git(root, "add", "src/branch.py")
    _git(root, "commit", "-q", "-m", "main child")
    tree = _git(root, "rev-parse", f"{common_parent}^{{tree}}")
    divergent = _git(
        root,
        "commit-tree",
        tree,
        "-p",
        common_parent,
        input_text="divergent child\n",
    )

    with pytest.raises(gate.LocBaselineError, match="not an ancestor"):
        gate.verify_baseline_provenance(root, _baseline(divergent))


def test_missing_recorded_path_fails_closed(tmp_path: Path) -> None:
    root, base_sha = _repo(tmp_path)
    baseline = _baseline(base_sha)
    baseline["oversized_python"]["files"][0]["file"] = "src/missing.py"

    with pytest.raises(gate.LocBaselineError, match="missing from base_git_sha"):
        gate.verify_baseline_provenance(root, baseline)


def test_threshold_cannot_be_raised_in_the_same_change(tmp_path: Path) -> None:
    root, base_sha = _repo(tmp_path)
    baseline = _baseline(base_sha)
    baseline["thresholds"]["python_new_file_max"] = 900

    with pytest.raises(gate.LocBaselineError, match="must remain 800"):
        gate.verify_baseline_provenance(root, baseline)
