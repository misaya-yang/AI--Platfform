from __future__ import annotations

import datetime as dt
from pathlib import Path

from scripts.harness.hygiene_check import apply_allowlist, run, scan_js, scan_python


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_python_syntax_error_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_broken.py", "def test_broken(:\n    pass\n")

    failures, warnings = scan_python(tmp_path)

    assert warnings == []
    assert failures == [
        {
            "file": "tests/test_broken.py",
            "line": 1,
            "test": "<module>",
            "issue": "python syntax error",
        }
    ]
    assert run(tmp_path, tmp_path / "evidence.json", tmp_path / "missing-allowlist.json") == 1


def test_python_ast_flags_empty_and_self_proving_bodies_only(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "tests/test_shapes.py",
        "import pytest\n\n"
        "def test_empty():\n"
        "    pass\n\n"
        "def test_literal_truth():\n"
        "    assert True\n\n"
        "def test_literal_comparison():\n"
        "    assert 1 < 2 < 3\n\n"
        "def test_identity(value):\n"
        "    assert value is value\n\n"
        "@pytest.mark.parametrize('value', [1])\n"
        "def test_parametrized_self_proof(value):\n"
        "    assert True\n\n"
        "def test_real(result):\n"
        "    assert result == 2\n\n"
        "def test_helper_only():\n"
        "    helper()\n",
    )

    failures, warnings = scan_python(tmp_path)

    issues = {(item["test"], item["issue"]) for item in failures}
    assert issues == {
        ("test_empty", "empty test body"),
        ("test_literal_truth", "self-proving assertion-only test"),
        ("test_literal_comparison", "self-proving assertion-only test"),
        ("test_identity", "self-proving assertion-only test"),
        ("test_parametrized_self_proof", "self-proving assertion-only test"),
    }
    assert [item["test"] for item in warnings] == ["test_helper_only"]


def test_typescript_full_file_scan_catches_multiline_and_placeholders(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "web/src/multiline.test.ts",
        "test\n"
        "  .only(\"focused\", () => { expect(1).toBe(1); });\n"
        "describe\n"
        "  .fixme(\"disabled\", () => { expect(1).toBe(1); });\n"
        "test(\n"
        "  \"comment placeholder\",\n"
        "  async ({ page }) => {\n"
        "    // TODO: exercise page\n"
        "  },\n"
        ");\n"
        "it(\"function placeholder\", function () {\n"
        "  /* intentionally empty for now */\n"
        "});\n"
        "test(\"real\", async () => { expect(1).toBe(1); });\n",
    )

    failures = scan_js(tmp_path)

    assert [item["issue"] for item in failures] == [
        ".only( focused test",
        ".fixme( disabled test",
        "empty test body",
        "empty test body",
    ]
    assert [item.get("test") for item in failures[-2:]] == [
        "comment placeholder",
        "function placeholder",
    ]


def test_dated_allowlist_match_expiry_and_staleness_are_preserved() -> None:
    failures = [
        {
            "file": "tests/test_backlog.py",
            "line": 1,
            "test": "test_allowed",
            "issue": "empty test body",
        },
        {
            "file": "tests/test_backlog.py",
            "line": 2,
            "test": "test_expired",
            "issue": "empty test body",
        },
    ]
    entries = [
        {
            "file": "tests/test_backlog.py",
            "test": "test_allowed",
            "owner": "ARC-07",
            "reason": "dated backlog",
            "expires": "2026-09-01",
        },
        {
            "file": "tests/test_backlog.py",
            "test": "test_expired",
            "owner": "ARC-07",
            "reason": "dated backlog",
            "expires": "2026-08-01",
        },
        {
            "file": "tests/test_backlog.py",
            "test": "test_stale",
            "owner": "ARC-07",
            "reason": "dated backlog",
            "expires": "2026-09-01",
        },
    ]

    enforced, allowlisted, expired, stale = apply_allowlist(
        failures, entries, dt.date(2026, 8, 30)
    )

    assert enforced == []
    assert [item["test"] for item in allowlisted] == ["test_allowed"]
    assert [item["test"] for item in expired] == ["test_expired"]
    assert [item["test"] for item in stale] == ["test_stale"]
