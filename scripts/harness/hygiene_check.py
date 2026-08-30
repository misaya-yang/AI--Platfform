#!/usr/bin/env python3
"""Minimal test-hygiene gate (L0).

Fail-closed checks:
  Python (test_*.py under tests/, sdk/, apps/, packages/):
    - empty test bodies (only pass/Ellipsis/docstring)
    - @pytest.mark.skip without a reason= (skipif is fine: the condition is the reason)
    - pytest.skip() calls with no message
  TypeScript (web/src, web/e2e, sdk *.test.* / *.spec.*):
    - .only(  — a focused test left behind makes CI run a subset silently
    - .fixme( — a disabled test masquerading as coverage
    - test("...") callbacks with an empty body

Informational (warning only, listed in the evidence so the bar can tighten):
  - Python test functions without an assertion token. Many legitimate tests
    assert through helpers today, so this is reported, not failed.

Pre-existing backlog may be suppressed through
``scripts/harness/hygiene_allowlist.json``; every entry needs an owner, a
reason and an expiry date and must name the exact test. Expired or stale
(no longer matching) entries fail the gate, so exceptions cannot outlive
their cleanup.

Usage:
  python3 scripts/harness/hygiene_check.py            # real gate
  python3 scripts/harness/hygiene_check.py --selftest # synthetic violations

Exit codes: 0 clean, 1 violations, 2 gate error.
Evidence: tmp/gate-evidence/hygiene-check.json
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "hygiene_allowlist.json"
DEFAULT_EVIDENCE = ROOT / "tmp" / "gate-evidence" / "hygiene-check.json"

PY_SCAN_ROOTS = ("tests", "sdk", "apps", "packages")
JS_SCAN_ROOTS = ("web", "sdk")
PRUNED_DIRS = {
    "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".playwright",
    "test-results", "playwright-report", "tmp", "target",
}
ASSERTION_TOKENS = ("assert", "raises(", "warns(", "fail(", "expect(")


def _iter_files(roots: tuple[str, ...], base: Path, suffixes: tuple[str, ...]):
    for root in roots:
        start = base / root
        if not start.is_dir():
            continue
        stack = [start]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in PRUNED_DIRS:
                        stack.append(entry)
                elif entry.suffix in suffixes:
                    yield entry


def _py_test_functions(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return  # unparseable files are not this gate's job
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node, source


def _empty_body(node: ast.AST) -> bool:
    body = [
        stmt
        for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant))
    ]
    if not body:
        return True
    return all(
        isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr) and stmt.value is Ellipsis)
        for stmt in body
    )


def _skip_decorator_without_reason(node) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "skip":
            value = target.value
            if isinstance(value, ast.Attribute) and value.attr == "mark":
                if not isinstance(decorator, ast.Call):
                    return True  # bare @pytest.mark.skip
                if not any(keyword.arg == "reason" for keyword in decorator.keywords):
                    return True
    return False


def scan_python(base: Path) -> tuple[list[dict], list[dict]]:
    failures: list[dict] = []
    warnings: list[dict] = []
    for path in _iter_files(PY_SCAN_ROOTS, base, (".py",)):
        if not path.name.startswith("test_"):
            continue
        rel = str(path.relative_to(base))
        for node, source in _py_test_functions(path):
            if _empty_body(node):
                failures.append({"file": rel, "line": node.lineno, "test": node.name, "issue": "empty test body"})
                continue
            if _skip_decorator_without_reason(node):
                failures.append({"file": rel, "line": node.lineno, "test": node.name, "issue": "@pytest.mark.skip without reason"})
            segment = ast.get_source_segment(source, node) or ""
            if re.search(r"pytest\.skip\(\s*\)", segment):
                failures.append({"file": rel, "line": node.lineno, "test": node.name, "issue": "pytest.skip() without a message"})
            if not any(token in segment for token in ASSERTION_TOKENS):
                warnings.append({"file": rel, "line": node.lineno, "test": node.name, "issue": "no assertion token found"})
    return failures, warnings


JS_ONLY = re.compile(r"\b(?:test|it|describe)\.only\s*\(")
JS_FIXME = re.compile(r"\b(?:test|it|describe)\.fixme\s*\(")
JS_EMPTY_BODY = re.compile(
    r"\b(?:test|it)\s*\(\s*[\"'`][^\"'`]*[\"'`]\s*,\s*(?:async\s*)?(?:\(\s*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{\s*\}"
)


def scan_js(base: Path) -> list[dict]:
    failures: list[dict] = []
    for path in _iter_files(JS_SCAN_ROOTS, base, (".ts", ".tsx")):
        if not re.search(r"\.(test|spec)\.tsx?$", path.name):
            continue
        rel = str(path.relative_to(base))
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if JS_ONLY.search(line):
                failures.append({"file": rel, "line": lineno, "issue": ".only( focused test"})
            elif JS_FIXME.search(line):
                failures.append({"file": rel, "line": lineno, "issue": ".fixme( disabled test"})
            elif JS_EMPTY_BODY.search(line):
                failures.append({"file": rel, "line": lineno, "issue": "empty test body"})
    return failures


def load_allowlist(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    for entry in entries:
        for key in ("file", "test", "owner", "reason", "expires"):
            if key not in entry:
                raise ValueError(f"allowlist entry missing '{key}': {entry!r}")
        dt.date.fromisoformat(entry["expires"])  # validates format
    return entries


def apply_allowlist(
    failures: list[dict], entries: list[dict], today: dt.date
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Split failures into enforced vs allowlisted; find stale/expired entries."""
    enforced: list[dict] = []
    allowlisted: list[dict] = []
    expired: list[dict] = []
    matched_entry_ids: set[int] = set()
    for failure in failures:
        matched = None
        for index, entry in enumerate(entries):
            if entry["file"] == failure["file"] and entry["test"] == failure.get("test"):
                matched = (index, entry)
                break
        if matched is None:
            enforced.append(failure)
            continue
        index, entry = matched
        matched_entry_ids.add(index)
        record = {**failure, "allowlist_owner": entry["owner"], "expires": entry["expires"]}
        if dt.date.fromisoformat(entry["expires"]) < today:
            expired.append(record)
        else:
            allowlisted.append(record)
    stale = [
        {"file": entry["file"], "test": entry["test"], "owner": entry["owner"]}
        for index, entry in enumerate(entries)
        if index not in matched_entry_ids
    ]
    return enforced, allowlisted, expired, stale


def run(base: Path, evidence_path: Path, allowlist_path: Path) -> int:
    try:
        entries = load_allowlist(allowlist_path) if allowlist_path.exists() else []
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"GATE ERROR: bad allowlist {allowlist_path}: {exc}", file=sys.stderr)
        return 2

    py_failures, py_warnings = scan_python(base)
    js_failures = scan_js(base)
    enforced, allowlisted, expired, stale = apply_allowlist(
        py_failures + js_failures, entries, dt.date.today()
    )
    result = {
        "gate": "hygiene-check",
        "tier": "L0",
        "failures": enforced,
        "allowlisted": allowlisted,
        "expired_entries": expired,
        "stale_entries": stale,
        "no_assertion_warnings": py_warnings,
        "result": "pass" if not (enforced or expired or stale) else "fail",
    }
    if base == ROOT:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"hygiene failures: {len(enforced)}; allowlisted: {len(allowlisted)}; "
        f"expired entries: {len(expired)}; stale entries: {len(stale)}; "
        f"no-assertion warnings: {len(py_warnings)}"
    )
    for item in enforced:
        test = item.get("test")
        where = f"{item['file']}:{item['line']}" + (f" {test}" if test else "")
        print(f"  FAIL {where}: {item['issue']}")
    for item in allowlisted:
        print(
            f"  allowlisted {item['file']} {item.get('test')} "
            f"(owner={item['allowlist_owner']}, expires={item['expires']})"
        )
    for item in expired:
        print(
            f"  EXPIRED allowlist entry still needed: {item['file']} {item.get('test')} "
            f"(owner={item['allowlist_owner']}, expired {item['expires']})"
        )
    for entry in stale:
        print(f"  STALE allowlist entry matches nothing: {entry['file']} {entry['test']}")
    for item in py_warnings[:20]:
        print(f"  WARN {item['file']}:{item['line']} {item['test']}: {item['issue']}")
    if len(py_warnings) > 20:
        print(f"  ... and {len(py_warnings) - 20} more no-assertion warnings (see evidence)")
    if enforced or expired or stale:
        return 1
    print(f"OK: test hygiene intact (evidence: {evidence_path.relative_to(base) if base == ROOT else 'selftest'})")
    return 0


def _selftest() -> int:
    layout = {
        "tests/test_bad_empty.py": "def test_nothing():\n    pass\n",
        "tests/test_bad_docstring_only.py": "def test_nothing():\n    \"\"\"only docs.\"\"\"\n",
        "tests/test_bad_skip_no_reason.py": (
            "import pytest\n\n@pytest.mark.skip\ndef test_skipped():\n    assert True\n"
        ),
        "tests/test_bad_skip_no_message.py": (
            "import pytest\n\ndef test_skipped():\n    pytest.skip()\n"
        ),
        "tests/test_good.py": (
            "import pytest\n\n"
            "@pytest.mark.skip(reason='needs live stack')\n"
            "def test_live():\n    assert True\n\n"
            "@pytest.mark.skipif(True, reason='conditional')\n"
            "def test_cond():\n    assert True\n\n"
            "def test_real():\n    assert 1 + 1 == 2\n\n"
            "def helper():\n    pass\n"
        ),
        "web/src/example.test.ts": (
            'test("focused", async () => { expect(1).toBe(1); });\n'
            'test.only("left behind", () => {});\n'
            'test.fixme("disabled", () => {});\n'
            'test("empty body", () => {});\n'
        ),
        "web/src/example.good.spec.ts": 'test("ok", () => { expect(1).toBe(1); });\n',
    }
    failures_seen: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        for rel, content in layout.items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        py_failures, py_warnings = scan_python(base)
        js_failures = scan_js(base)
        all_failures = py_failures + js_failures
        failures_seen = [f"{f['file']}:{f['issue']}" for f in all_failures]

    expected = [
        "tests/test_bad_empty.py:empty test body",
        "tests/test_bad_docstring_only.py:empty test body",
        "tests/test_bad_skip_no_reason.py:@pytest.mark.skip without reason",
        "tests/test_bad_skip_no_message.py:pytest.skip() without a message",
        "web/src/example.test.ts:.only( focused test",
        "web/src/example.test.ts:.fixme( disabled test",
        "web/src/example.test.ts:empty test body",
    ]
    missing = [e for e in expected if e not in failures_seen]
    extra = [f for f in failures_seen if f not in expected]
    if missing or extra:
        print(f"SELFTEST FAILED\n  missing: {missing}\n  unexpected: {extra}", file=sys.stderr)
        return 1
    clean = [f["file"] for f in all_failures if f["file"].startswith(("tests/test_good", "web/src/example.good"))]
    if clean:
        print(f"SELFTEST FAILED: clean files flagged: {clean}", file=sys.stderr)
        return 1

    # Allowlist behaviour: match + expiry + staleness.
    entries = [
        {"file": "tests/test_bad_empty.py", "test": "test_nothing",
         "owner": "selftest", "reason": "synthetic", "expires": "2999-01-01"},
        {"file": "tests/test_bad_skip_no_reason.py", "test": "test_skipped",
         "owner": "selftest", "reason": "expired entry must fail", "expires": "2000-01-01"},
        {"file": "tests/does_not_exist.py", "test": "test_ghost",
         "owner": "selftest", "reason": "stale entry must fail", "expires": "2999-01-01"},
    ]
    enforced, allowlisted, expired, stale = apply_allowlist(all_failures, entries, dt.date.today())
    if [f["file"] for f in allowlisted] != ["tests/test_bad_empty.py"]:
        print(f"SELFTEST FAILED: allowlist match wrong: {allowlisted}", file=sys.stderr)
        return 1
    if [f["file"] for f in expired] != ["tests/test_bad_skip_no_reason.py"]:
        print(f"SELFTEST FAILED: expired entry not flagged: {expired}", file=sys.stderr)
        return 1
    if [e["file"] for e in stale] != ["tests/does_not_exist.py"]:
        print(f"SELFTEST FAILED: stale entry not flagged: {stale}", file=sys.stderr)
        return 1
    if any(f["file"] == "tests/test_bad_empty.py" for f in enforced):
        print("SELFTEST FAILED: allowlisted finding still enforced", file=sys.stderr)
        return 1
    print(f"SELFTEST OK: {len(expected)} synthetic hygiene violations detected, clean files untouched, "
          "allowlist match/expiry/staleness enforced")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--evidence-out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.root.is_dir():
        print(f"GATE ERROR: root not found: {args.root}", file=sys.stderr)
        return 2
    return run(args.root, args.evidence_out, args.allowlist)


if __name__ == "__main__":
    raise SystemExit(main())
