"""Shared helpers for the ARC-00 fact-baseline generators.

Stdlib-only on purpose (same rule as ``scripts/harness/check_harness.py``):
the baseline must be regenerable on any machine that has Python 3.10+ and a
checkout of this repository, without installing the uv workspace.

Determinism contract: every generator sorts its output, never embeds
wall-clock time, and derives its identity fields from the Git revision and
file contents only. Re-running the generator at the same Git revision must
produce byte-identical JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

# scripts/inventory/_common.py -> parents[2] is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

OUTPUT_DIR = REPO_ROOT / "docs" / "architecture" / "baselines" / "2026-08-post-rag"

BASELINE_ID = "2026-08-post-rag"

# Directories that never contain first-party source facts.
PRUNED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".playwright",
    ".claude",
    ".worktrees",
    "tmp",
    "temp",
    "logs",
    "uploads",
    "test-results",
    "playwright-report",
    "htmlcov",
    "target",  # Rust build output
}

# Source units that own Python code. Maps top-level path -> unit id.
PYTHON_UNITS = {
    "src": "gateway",
    "apps/knowledge-service": "knowledge-service",
    "apps/local-node": "local-node",
    "packages/ai-gateway-core": "ai-gateway-core",
    "sdk/python": "sdk-python",
    "scripts": "scripts",
    "tests": "tests",
    "database": "database-tools",
}

# Importable module name -> owning unit (for cross-package import detection).
MODULE_UNITS = {
    "src": "gateway",
    "knowledge_service": "knowledge-service",
    "local_node": "local-node",
    "ai_gateway_core": "ai-gateway-core",
}


def git_head_sha() -> str:
    """Current Git revision; the baseline's identity anchor."""
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def walk_files(suffixes: tuple[str, ...], roots: tuple[str, ...] | None = None):
    """Yield repo-relative paths with the given suffixes, pruned and sorted."""
    scan_roots = [REPO_ROOT / r for r in (roots or tuple(PYTHON_UNITS))]
    found: list[Path] = []
    for root in scan_roots:
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except (PermissionError, OSError):
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in PRUNED_DIRS:
                        stack.append(entry)
                elif entry.suffix in suffixes:
                    found.append(entry)
    return sorted(set(found))


def unit_for_path(path: Path) -> str:
    """Map a repo-relative path to its owning source unit."""
    rel = str(path)
    for prefix in sorted(PYTHON_UNITS, key=len, reverse=True):
        if rel == prefix or rel.startswith(prefix + "/"):
            return PYTHON_UNITS[prefix]
    return "other"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_sha256(obj) -> str:
    """SHA-256 of the canonical JSON encoding of ``obj`` (stable across runs)."""
    return sha256_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def write_json(name: str, payload: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / name
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def base_envelope(kind: str) -> dict:
    """Common identity block shared by every baseline file."""
    return {
        "schema": f"ai-gateway/baseline/{kind}/v1",
        "baseline_id": BASELINE_ID,
        "base_git_sha": git_head_sha(),
        "generator": "scripts/inventory/generate_baselines.py",
    }


_SQL_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE)\s+(?:ONLY\s+)?"
    r"((?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)

_SQL_KEYWORDS = {
    "select", "where", "group", "order", "limit", "offset", "returning", "set",
    "values", "distinct", "lateral", "generate_series", "unnest", "jsonb",
    "json", "information_schema", "pg_catalog",
}


def extract_sql_table_refs(sql_text: str) -> set[str]:
    """Best-effort table references from a SQL fragment (static scan)."""
    refs: set[str] = set()
    for match in _SQL_TABLE_REF.finditer(sql_text):
        name = match.group(1).lower().strip(".")
        leaf = name.rsplit(".", 1)[-1]
        if leaf in _SQL_KEYWORDS or name in _SQL_KEYWORDS:
            continue
        refs.add(name)
    return refs
