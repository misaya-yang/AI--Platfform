#!/usr/bin/env python3
"""Assistant-service dependency inventory analyzer (Phase 1 of isolation plan).

Walks every .py file under src/services/assistant/, parses AST, and records
every import that escapes the assistant module. Resolves relative imports to
absolute dotted paths using the source file's package.

Output: plans/assistant-deps-inventory.json
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSISTANT_ROOT = REPO_ROOT / "src" / "services" / "assistant"
ASSISTANT_PKG_PREFIX = "src.services.assistant"
OUTPUT_JSON = REPO_ROOT / "plans" / "assistant-deps-inventory.json"


def file_to_package(file_path: Path) -> str:
    """Convert a .py file path (relative to REPO_ROOT) to its dotted package.

    The file's package is the dotted path of its containing directory,
    e.g. src/services/assistant/agent/agent_loop.py -> src.services.assistant.agent
    For __init__.py the package IS the directory itself.
    """
    rel = file_path.relative_to(REPO_ROOT)
    parts = list(rel.parts)
    # drop trailing "file.py"
    if parts[-1].endswith(".py"):
        parts = parts[:-1]
    return ".".join(parts)


def resolve_relative(module: str | None, level: int, pkg: str) -> str:
    """Resolve a relative import to an absolute dotted module.

    Args:
        module: the X in `from .X import Y`, or None for `from . import Y`
        level: number of leading dots
        pkg: the importing file's package (dotted)

    Returns:
        absolute dotted module path
    """
    parts = pkg.split(".")
    # level=1 means "current package"; level=N means go up N-1 parents
    if level > len(parts):
        # Over-dotted relative — shouldn't happen in a well-formed repo,
        # fall back to joining what we have
        base_parts: list[str] = []
    else:
        base_parts = parts[: len(parts) - (level - 1)]
    if module:
        base_parts = base_parts + module.split(".")
    return ".".join(p for p in base_parts if p)


def is_external(abs_module: str) -> bool:
    """True iff resolved module is in src.* but outside assistant tree."""
    if not abs_module.startswith("src."):
        return False
    if abs_module == ASSISTANT_PKG_PREFIX:
        return False
    if abs_module.startswith(ASSISTANT_PKG_PREFIX + "."):
        return False
    return True


@dataclass
class ImportEntry:
    target_module: str
    symbol: str  # imported name, or "*" for `import X`
    source_files: set[str] = field(default_factory=set)


def classify(target_module: str) -> tuple[str, str]:
    """Auto-classify a target module into a bucket. Rules applied top-to-bottom."""
    tm = target_module
    if tm == "src.core.observability" or tm.startswith("src.core.observability."):
        return "shared", "src.core.observability.* — pure logging utilities"
    if tm == "src.core.exceptions" or tm.startswith("src.core.exceptions."):
        return "shared", "src.core.exceptions.* — exception classes, no infra"
    if tm == "src.models.enums" or tm.startswith("src.models.enums."):
        return "shared", "src.models.enums.* — pure enum types"
    if tm == "src.models.schemas" or tm.startswith("src.models.schemas."):
        return "shared", "src.models.schemas.* — pure data schemas"
    if tm == "src.core.auth.user_resolver":
        return "contract", "user_resolver — protocol/contract, not concrete impl"
    if tm == "src.persistence.database" or tm.startswith("src.persistence.database."):
        return "contract", "persistence.database — DB client protocol boundary"
    if tm == "src.services.session.database_session_manager" or tm.startswith(
        "src.services.session.database_session_manager."
    ):
        return "contract", "database_session_manager — operator to decide shared vs. move"
    if tm == "src.services.knowledge.kb_proxy_client" or tm.startswith(
        "src.services.knowledge.kb_proxy_client."
    ):
        return "shared", "kb_proxy_client — moved to ai_gateway_core.knowledge.proxy_client (Phase 5f Batch C); src/ path is a shim"
    if tm == "src.services.knowledge" or tm.startswith("src.services.knowledge."):
        return "replace", "src.services.knowledge.* — must be rewritten to KBProxyClient HTTP call"
    if tm == "src.services.metrics" or tm.startswith("src.services.metrics."):
        return "contract", "metrics — observability contract; both services want to record"
    if tm == "src.services.storage" or tm.startswith("src.services.storage."):
        return "contract", "storage — artifact/file storage contract; impl differs per service"
    if tm == "src.runtime" or tm.startswith("src.runtime."):
        return "move", "src.runtime.* — assistant-owned, dedup candidate"
    return "review", "unclassified — operator must reclassify before Phase 2"


def walk_and_collect() -> dict[tuple[str, str], ImportEntry]:
    entries: dict[tuple[str, str], ImportEntry] = {}
    unresolved: list[tuple[str, str]] = []

    py_files = sorted(ASSISTANT_ROOT.rglob("*.py"))
    for fp in py_files:
        if "__pycache__" in fp.parts:
            continue
        pkg = file_to_package(fp)
        try:
            src_text = fp.read_text(encoding="utf-8")
            tree = ast.parse(src_text, filename=str(fp))
        except (SyntaxError, UnicodeDecodeError) as exc:
            unresolved.append((str(fp), f"parse-error: {exc}"))
            continue

        rel_path = str(fp.relative_to(REPO_ROOT))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                level = node.level or 0
                mod = node.module  # may be None
                if level > 0:
                    abs_mod = resolve_relative(mod, level, pkg)
                else:
                    abs_mod = mod or ""
                if not abs_mod:
                    continue
                if not is_external(abs_mod):
                    continue
                for alias in node.names:
                    sym = alias.name
                    key = (abs_mod, sym)
                    ent = entries.get(key)
                    if ent is None:
                        ent = ImportEntry(target_module=abs_mod, symbol=sym)
                        entries[key] = ent
                    ent.source_files.add(rel_path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    abs_mod = alias.name
                    if not is_external(abs_mod):
                        continue
                    key = (abs_mod, "*")
                    ent = entries.get(key)
                    if ent is None:
                        ent = ImportEntry(target_module=abs_mod, symbol="*")
                        entries[key] = ent
                    ent.source_files.add(rel_path)

    if unresolved:
        print(f"  WARN: {len(unresolved)} files failed to parse:")
        for f, why in unresolved[:5]:
            print(f"    {f}: {why}")

    return entries


def build_inventory(entries: dict[tuple[str, str], ImportEntry]) -> dict:
    out_entries = []
    bucket_counts: dict[str, int] = defaultdict(int)
    total_calls = 0

    for (tm, sym), ent in entries.items():
        bucket, reason = classify(tm)
        bucket_counts[bucket] += 1
        files_sorted = sorted(ent.source_files)
        out_entries.append(
            {
                "target_module": tm,
                "symbol": sym,
                "call_sites_count": len(ent.source_files),
                "source_files": files_sorted[:20],
                "bucket": bucket,
                "bucket_reason": reason,
            }
        )
        total_calls += len(ent.source_files)

    bucket_order = {"shared": 0, "contract": 1, "move": 2, "replace": 3, "review": 4}
    out_entries.sort(key=lambda e: (bucket_order.get(e["bucket"], 99), e["target_module"], e["symbol"]))

    unique_targets = len({e["target_module"] for e in out_entries})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analyzer_version": "1.0",
        "source_root": "src/services/assistant",
        "total_external_imports": total_calls,
        "total_unique_targets": unique_targets,
        "counts_by_bucket": {
            "shared": bucket_counts.get("shared", 0),
            "contract": bucket_counts.get("contract", 0),
            "move": bucket_counts.get("move", 0),
            "replace": bucket_counts.get("replace", 0),
            "review": bucket_counts.get("review", 0),
        },
        "entries": out_entries,
    }


def main() -> None:
    print(f"Scanning {ASSISTANT_ROOT.relative_to(REPO_ROOT)}/ ...")
    entries = walk_and_collect()
    inventory = build_inventory(entries)

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {OUTPUT_JSON.relative_to(REPO_ROOT)}")
    print(f"  total_external_imports : {inventory['total_external_imports']}")
    print(f"  total_unique_targets   : {inventory['total_unique_targets']}")
    print(f"  unique (target,symbol) : {len(inventory['entries'])}")
    print("  counts_by_bucket:")
    for b, c in inventory["counts_by_bucket"].items():
        print(f"    {b:<8} : {c}")


if __name__ == "__main__":
    main()
