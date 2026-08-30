"""LOC baseline.

Produces ``loc-baseline.json``: recomputed line counts for the whole tree,
plus the oversized-file ledger (Python > 800 lines, TypeScript/TSX > 500
lines) that ARC-07/AC-M22 holds at no-growth.

The PRD's historical numbers are explicitly not reusable; this file is the
post-RAG truth at the pinned Git revision. Test files are counted separately
so production and test size pressure are not conflated.
"""

from __future__ import annotations

from pathlib import Path

from _common import REPO_ROOT, base_envelope, unit_for_path, walk_files

PY_THRESHOLD = 800
TS_THRESHOLD = 500

# Scan roots for source facts. tmp/, logs/, .venv etc. are pruned in common.
SCAN_ROOTS = ("src", "apps", "packages", "scripts", "database", "web", "sdk", "tests")


def _count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (UnicodeDecodeError, OSError):
        return 0


def _is_test_path(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return (
        rel.startswith("tests/")
        or "/tests/" in rel
        or "/e2e/" in rel
        or base.startswith("test_")
        or base.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
    )


def _oversized(files: list[Path], threshold: int) -> list[dict]:
    rows = []
    for rel in files:
        lines = _count_lines(REPO_ROOT / rel)
        if lines > threshold:
            rows.append(
                {
                    "file": str(rel),
                    "lines": lines,
                    "unit": unit_for_path(rel),
                    "is_test": _is_test_path(str(rel)),
                }
            )
    rows.sort(key=lambda item: (-item["lines"], item["file"]))
    return rows


def build() -> dict:
    py_files = walk_files((".py",), roots=SCAN_ROOTS)
    ts_files = walk_files((".ts", ".tsx"), roots=SCAN_ROOTS)
    rs_files = walk_files((".rs",), roots=("rust",))

    py_oversized = _oversized(py_files, PY_THRESHOLD)
    ts_oversized = _oversized(ts_files, TS_THRESHOLD)
    rs_large = _oversized(rs_files, 2000)  # informational only; pinned upstream, no LOC churn

    def totals(files: list[Path]) -> dict:
        lines = 0
        for rel in files:
            lines += _count_lines(REPO_ROOT / rel)
        return {"files": len(files), "lines": lines}

    production_py = [f for f in py_files if not _is_test_path(str(f))]
    production_ts = [f for f in ts_files if not _is_test_path(str(f))]

    def by_unit(rows: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["unit"]] = counts.get(row["unit"], 0) + 1
        return dict(sorted(counts.items()))

    return {
        **base_envelope("loc-baseline"),
        "thresholds": {
            "python_new_file_max": PY_THRESHOLD,
            "typescript_new_file_max": TS_THRESHOLD,
            "policy": "PRD AC-M22 / ARC-07E: oversized files are no-growth; new files stay under threshold; exceptions need owner + reason + expiry.",
        },
        "totals": {
            "python": totals(py_files),
            "python_production": totals(production_py),
            "typescript": totals(ts_files),
            "typescript_production": totals(production_ts),
            "rust_overlay": totals(rs_files),
        },
        "oversized_python": {
            "threshold_lines": PY_THRESHOLD,
            "count": len(py_oversized),
            "production_count": sum(1 for row in py_oversized if not row["is_test"]),
            "by_unit": by_unit(py_oversized),
            "files": py_oversized,
        },
        "oversized_typescript": {
            "threshold_lines": TS_THRESHOLD,
            "count": len(ts_oversized),
            "production_count": sum(1 for row in ts_oversized if not row["is_test"]),
            "by_unit": by_unit(ts_oversized),
            "files": ts_oversized,
        },
        "rust_large_informational": {
            "threshold_lines": 2000,
            "note": (
                "Pinned upstream Rust is not churned for LOC targets (PRD ARC-07E); "
                "listed only so reviewers know the shape of the overlay."
            ),
            "count": len(rs_large),
            "files": rs_large,
        },
        "methodology": [
            "wc-style line counts over the pruned tree at the pinned Git revision.",
            "Test files are classified by path (tests/, e2e/, *.test.ts, *.spec.ts, test_*.py).",
            "Re-running this generator at the same SHA must yield byte-identical output.",
        ],
    }


if __name__ == "__main__":
    from _common import write_json

    path = write_json("loc-baseline.json", build())
    print(f"wrote {path}")
