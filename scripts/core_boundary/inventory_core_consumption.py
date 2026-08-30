#!/usr/bin/env python3
"""ARC-04 import/data-access inventory for ``ai-gateway-core`` consumers.

Generates the machine-readable inventory required by PRD §ARC-04 goal 1:

- which ``ai_gateway_core`` modules the Gateway (``src/``) and Knowledge
  (``apps/knowledge-service/``) services actually import;
- which of those modules are I/O-free protocol candidates consumed by at
  least two owners (cross-boundary stable protocols);
- per-module direct third-party imports (I/O dependency evidence);
- table-level SQL access of the core persistence modules (read vs write);
- Rust-side schema-version markers sharing a wire contract with Python.

The committed JSON doubles as the no-growth baseline for
``check_core_boundary``.  Regenerate with::

    uv run python scripts/core_boundary/inventory_core_consumption.py

Output: ``reports/inventory/core-import-inventory.json`` (stable key order).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = "arc04-core-inventory/v2"

CORE_PKG_DIR = Path("packages/ai-gateway-core/src/ai_gateway_core")
CONTRACTS_PKG_DIR = Path("packages/ai-gateway-contracts/src/ai_gateway_contracts")

# Docstring marker every ARC-04 compatibility shim carries.  The inventory
# and the boundary gate use it to tell true shims apart from core modules
# that merely import a contracts helper (e.g. auth/gateway_secret.py).
SHIM_MARKER = "Compatibility shim — implementation moved to ``ai_gateway_contracts``"

# Directories scanned for consumers, mapped to an owner label.  Order matters:
# first match wins (e.g. packages/*/tests before packages/**).  ``core``
# tracks intra-package imports so the boundary gate can see exactly which
# core modules still reach into ai_gateway_contracts (shims + helpers).
CONSUMER_SCOPES: tuple[tuple[str, str], ...] = (
    ("src", "gateway"),
    ("apps/knowledge-service", "knowledge"),
    ("tests", "tests"),
    ("packages/ai-gateway-core/tests", "tests"),
    ("packages/ai-gateway-contracts/tests", "tests"),
    ("scripts", "scripts"),
    ("sdk/python", "sdk"),
    ("packages/ai-gateway-core/src", "core"),
)

# Third-party imports that prove a module performs I/O or embeds service
# configuration (i.e. is NOT an I/O-free protocol candidate).
IO_MARKER_IMPORTS: frozenset[str] = frozenset(
    {
        "asyncpg",
        "redis",
        "httpx",
        "aiohttp",
        "requests",
        "urllib",
        "fastapi",
        "starlette",
        "oss2",
        "aioboto3",
        "boto3",
        "aiofiles",
        "PIL",
        "numpy",
        "cryptography",
        "opentelemetry",
        "pydantic_settings",
        "dotenv",
        "dashscope",
        "qdrant_client",
        "yaml",
        "tiktoken",
        "websockets",
        "sse_starlette",
    }
)

# Schema-version strings shared between Python and Rust; presence in rust/
# proves the Rust side is a co-owner of the wire contract.
RUST_MARKERS: tuple[str, ...] = (
    "ai-platform-capability-proof/v1",
    "agent-runtime-model-lease/v1",
    "agent-runtime/v1",
    "agent-runtime-envelope/v1",
    "usage.recorded.v1",
)

_SQL_WRITE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO)\s+(?:ONLY\s+)?"
    r"([a-z_][a-z0-9_.]*)",
    re.IGNORECASE,
)
_SQL_READ = re.compile(
    r"\b(FROM|JOIN)\s+(?:ONLY\s+)?([a-z_][a-z0-9_.]*)",
    re.IGNORECASE,
)
_SQL_NOISE = frozenset(
    {"select", "where", "set", "values", "returning", "lateral", "unnest", "generate_series"}
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def base_sha(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def iter_python_files(root: Path, rel_dir: str) -> Iterable[Path]:
    base = root / rel_dir
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def owner_for(rel_path: Path) -> str | None:
    rel = rel_path.as_posix()
    for prefix, owner in CONSUMER_SCOPES:
        if rel == prefix or rel.startswith(prefix + "/"):
            return owner
    return None


def parse_imports(path: Path) -> tuple[set[str], set[str]]:
    """Return ``(plain_imports, from_modules)`` of dotted module paths.

    - ``plain_imports``: targets of ``import a.b`` statements.
    - ``from_modules``: source modules of ``from a.b import ...`` plus, for
      ``from pkg import name``, the deeper ``pkg.name`` when the imported
      symbol is itself a submodule (resolved by the caller against the known
      module catalog).
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set(), set()
    plain: set[str] = set()
    from_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            plain.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            if node.module == "ai_gateway_core":
                # `from ai_gateway_core import submodule_or_symbol` — resolve
                # per-symbol below against the known catalog.
                for alias in node.names:
                    if alias.name != "*":
                        from_modules.add(f"ai_gateway_core.{alias.name}")
            else:
                from_modules.add(node.module)
    return plain, from_modules


def package_modules(root: Path, pkg_dir: Path) -> dict[str, Path]:
    """Dotted module path -> file for every module of a workspace package."""
    modules: dict[str, Path] = {}
    src_root = root / pkg_dir.parent
    for path in iter_python_files(root, pkg_dir.as_posix()):
        rel = path.relative_to(src_root)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        modules[".".join(parts)] = path
    return modules


def core_modules(root: Path) -> dict[str, Path]:
    """Dotted module path -> file for every ai_gateway_core module."""
    return package_modules(root, CORE_PKG_DIR)


def direct_imports(path: Path) -> set[str]:
    """Third-party/stdlib top-level imports declared directly in this file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def sql_tables(path: Path) -> dict[str, list[str]]:
    """Extract table names referenced by SQL string constants in ``path``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return {"write": [], "read": []}
    write: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if len(text) < 12 or not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text, re.I):
                continue
            for match in _SQL_WRITE.finditer(text):
                table = match.group(2).lower()
                if table not in _SQL_NOISE:
                    write.add(table)
            for match in _SQL_READ.finditer(text):
                table = match.group(2).lower()
                if table not in _SQL_NOISE and "." not in table:
                    read.add(table)
    return {"write": sorted(write), "read": sorted(read)}


def build_inventory(root: Path) -> dict:
    modules = core_modules(root)
    known_submodules = set(modules)
    contracts = package_modules(root, CONTRACTS_PKG_DIR)
    known_contracts_submodules = set(contracts)

    consumption: dict[str, dict[str, dict[str, object]]] = {
        owner: {} for _, owner in CONSUMER_SCOPES
    }
    consumption.setdefault("other", {})
    contracts_consumption: dict[str, dict[str, dict[str, object]]] = {
        owner: {} for _, owner in CONSUMER_SCOPES
    }
    contracts_consumption.setdefault("other", {})
    core_import_paths: set[str] = set()
    contracts_import_paths: set[str] = set()

    def reduce(dotted: str, catalog: set[str]) -> str | None:
        """Reduce a dotted path to the longest known submodule in ``catalog``."""
        parts = dotted.split(".")
        for depth in range(len(parts), 0, -1):
            candidate = ".".join(parts[:depth])
            if candidate in catalog:
                return candidate
        return None

    def record(
        bucket_map: dict[str, dict[str, object]], owner: str, target: str, rel_posix: str
    ) -> None:
        bucket = bucket_map.setdefault(target, {"files": [], "count": 0})
        files = bucket["files"]
        assert isinstance(files, list)
        if rel_posix not in files:
            files.append(rel_posix)
            bucket["count"] = len(files)

    scan_dirs = [prefix for prefix, _ in CONSUMER_SCOPES] + ["database"]
    for rel_dir in scan_dirs:
        for path in iter_python_files(root, rel_dir):
            rel = path.relative_to(root)
            owner = owner_for(rel) or "other"
            plain, from_modules = parse_imports(path)
            targets: set[str] = set()
            contracts_targets: set[str] = set()
            for dotted in plain:
                if dotted.startswith("ai_gateway_core"):
                    target = reduce(dotted, known_submodules)
                    if target:
                        targets.add(target)
                        core_import_paths.add(dotted)
                elif dotted.startswith("ai_gateway_contracts"):
                    target = reduce(dotted, known_contracts_submodules)
                    if target:
                        contracts_targets.add(target)
                        contracts_import_paths.add(dotted)
            for dotted in from_modules:
                if dotted.startswith("ai_gateway_contracts"):
                    target = reduce(dotted, known_contracts_submodules)
                    if target is not None:
                        if target == "ai_gateway_contracts" and dotted != "ai_gateway_contracts":
                            continue
                        contracts_targets.add(target)
                        contracts_import_paths.add(dotted)
                    continue
                target = reduce(dotted, known_submodules)
                if target is None:
                    continue
                # `from ai_gateway_core import symbol` where symbol is not a
                # submodule collapses to the bare package — only keep it when
                # that is genuinely what was imported.
                if target == "ai_gateway_core" and dotted != "ai_gateway_core":
                    continue
                targets.add(target)
                core_import_paths.add(dotted)
            rel_posix = rel.as_posix()
            for target in targets:
                record(consumption[owner], owner, target, rel_posix)
            for target in contracts_targets:
                record(contracts_consumption[owner], owner, target, rel_posix)

    # Also catch bare `import ai_gateway_core` consumers.
    module_info: dict[str, dict] = {}
    for dotted in sorted(modules):
        path = modules[dotted]
        tops = direct_imports(path)
        io_deps = sorted(tops & IO_MARKER_IMPORTS)
        text = path.read_text(encoding="utf-8")
        is_shim = SHIM_MARKER in text
        owners = {
            owner: sorted(data[dotted]["files"])
            for owner, data in consumption.items()
            if dotted in data
        }
        module_info[dotted] = {
            "file": path.relative_to(root).as_posix(),
            "kind": "package" if path.name == "__init__.py" else "module",
            "loc": text.count("\n") + (0 if text.endswith("\n") else 1),
            "direct_imports": sorted(tops),
            "io_deps": io_deps,
            "io_free": not io_deps,
            "contracts_shim": is_shim,
            "consumer_owners": owners,
        }

    # Table access for persistence-adjacent modules.
    table_access: dict[str, dict[str, list[str]]] = {}
    for dotted, path in modules.items():
        if dotted.startswith(("ai_gateway_core.persistence", "ai_gateway_core.session")):
            tables = sql_tables(path)
            if tables["write"] or tables["read"]:
                table_access[dotted] = tables

    # Contracts package module info — the allowlist gate compares against this.
    contracts_info: dict[str, dict] = {}
    for dotted in sorted(contracts):
        path = contracts[dotted]
        tops = direct_imports(path)
        io_deps = sorted(tops & IO_MARKER_IMPORTS)
        text = path.read_text(encoding="utf-8")
        contracts_info[dotted] = {
            "file": path.relative_to(root).as_posix(),
            "kind": "package" if path.name == "__init__.py" else "module",
            "loc": text.count("\n") + (0 if text.endswith("\n") else 1),
            "direct_imports": sorted(tops),
            "io_deps": io_deps,
            "io_free": not io_deps,
        }

    knowledge_modules = sorted(consumption["knowledge"])
    rust_markers = {marker: sorted(_rust_marker_files(root, marker)) for marker in RUST_MARKERS}
    # PRD §ARC-04 goal 1: only I/O-free protocols consumed by at least two
    # owners qualify for ai-gateway-contracts.  Leaf modules are protocol
    # candidates; package __init__ entries (logging/proxy/tracing/…) are
    # shared infrastructure and stay in core per goal 3.
    cross_boundary: list[dict[str, object]] = []
    shared_infra: list[str] = []
    for dotted, info in sorted(module_info.items()):
        service_owners = sorted(
            owner for owner in info["consumer_owners"] if owner in ("gateway", "knowledge")
        )
        if len(service_owners) < 2 or not info["io_free"]:
            continue
        if info["kind"] == "package":
            shared_infra.append(dotted)
            continue
        marker = dotted_to_marker(dotted)
        rust_files = rust_markers.get(marker, []) if marker else []
        cross_boundary.append(
            {
                "module": dotted,
                "service_owners": service_owners,
                "rust_marker": marker if rust_files else None,
                "rust_files": rust_files,
                "in_contracts": bool(core_to_contracts(dotted)),
            }
        )

    # Shim consumption baseline: files importing through compat paths that
    # re-export from ai_gateway_contracts (only meaningful post-migration).
    shim_modules = sorted(m for m, i in module_info.items() if i["contracts_shim"])
    shim_consumers = {
        m: {
            "files": sorted(
                f
                for owner in ("gateway", "knowledge", "tests", "scripts", "sdk", "core", "other")
                for f in module_info[m]["consumer_owners"].get(owner, [])
            )
        }
        for m in shim_modules
    }
    for m in shim_modules:
        shim_consumers[m]["count"] = len(shim_consumers[m]["files"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_sha": base_sha(root),
        "core_package_dir": CORE_PKG_DIR.as_posix(),
        "contracts_package_dir": CONTRACTS_PKG_DIR.as_posix(),
        "modules": module_info,
        "contracts_modules": contracts_info,
        "consumption": {
            owner: {m: consumption[owner][m] for m in sorted(consumption[owner])}
            for owner in consumption
        },
        "contracts_consumption": {
            owner: {
                m: contracts_consumption[owner][m] for m in sorted(contracts_consumption[owner])
            }
            for owner in contracts_consumption
        },
        "knowledge_core_modules": knowledge_modules,
        "knowledge_core_module_count": len(knowledge_modules),
        "gateway_core_module_count": len(consumption["gateway"]),
        "cross_boundary_protocol_candidates": cross_boundary,
        "shared_infra_packages": shared_infra,
        "rust_markers": rust_markers,
        "table_access": table_access,
        "shim_consumers": shim_consumers,
        "core_import_paths_seen": sorted(core_import_paths),
        "contracts_import_paths_seen": sorted(contracts_import_paths),
    }


def dotted_to_marker(dotted: str) -> str | None:
    """Map a core module to its shared schema-version marker, when known."""
    mapping = {
        "ai_gateway_core.auth.capability_proof": "ai-platform-capability-proof/v1",
        "ai_gateway_core.agents.runtime_lease": "agent-runtime-model-lease/v1",
        "ai_gateway_core.agents.runtime": "agent-runtime-envelope/v1",
        "ai_gateway_core.events.envelope": "usage.recorded.v1",
    }
    return mapping.get(dotted)


# Core shim module -> contracts module owning the implementation.  Keep in
# sync with check_core_boundary.CONTRACTS_ALLOWLIST: the gate uses this map
# as the authoritative shim list.
CORE_TO_CONTRACTS: dict[str, str] = {
    "ai_gateway_core.auth.capability_proof": "ai_gateway_contracts.capability_proof",
    "ai_gateway_core.agents.runtime": "ai_gateway_contracts.agent_runtime",
    "ai_gateway_core.agents.runtime_lease": "ai_gateway_contracts.agent_runtime_lease",
    "ai_gateway_core.events.envelope": "ai_gateway_contracts.event_envelope",
    "ai_gateway_core.events.errors": "ai_gateway_contracts.event_errors",
}


def core_to_contracts(dotted: str) -> str | None:
    """Return the contracts module a migrated core shim re-exports, if any."""
    return CORE_TO_CONTRACTS.get(dotted)


def _rust_marker_files(root: Path, marker: str) -> Iterable[str]:
    rust_dir = root / "rust"
    if not rust_dir.is_dir():
        return
    needle = marker.encode("utf-8")
    for path in sorted(rust_dir.rglob("*.rs")):
        try:
            if needle in path.read_bytes():
                yield path.relative_to(root).as_posix()
        except OSError:
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="reports/inventory/core-import-inventory.json",
        help="inventory JSON destination (repo-relative)",
    )
    args = parser.parse_args(argv)
    root = repo_root()
    inventory = build_inventory(root)
    out_path = root / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {out_path.relative_to(root)}: "
        f"{len(inventory['modules'])} core modules, "
        f"{len(inventory['contracts_modules'])} contracts modules, "
        f"gateway consumes {inventory['gateway_core_module_count']}, "
        f"knowledge consumes {inventory['knowledge_core_module_count']}, "
        f"cross-boundary candidates: "
        f"{len(inventory['cross_boundary_protocol_candidates'])}, "
        f"shims: {len(inventory['shim_consumers'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
