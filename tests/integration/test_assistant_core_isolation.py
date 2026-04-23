"""Phase 4 exit-gate guard — locks in the isolation invariant.

Asserts:

1. ``apps/assistant-service/src/assistant_service/core/`` contains ZERO
   ``from src.`` imports. Core business logic must reach gateway-owned
   concretes through the Protocols in ``ai_gateway_core`` or through DI
   from the composition root (``main.py``).

2. The only file in ``apps/assistant-service/`` allowed to import from
   ``src.`` is ``main.py``. Any other file regressing to a direct
   ``src.*`` import will fail this test.

These are the two hard invariants that keep Phase 4's work from rotting.
Removing this test is equivalent to undoing the migration.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPS_ROOT = REPO_ROOT / "apps" / "assistant-service"
CORE_ROOT = APPS_ROOT / "src" / "assistant_service" / "core"
MAIN_PY = APPS_ROOT / "src" / "assistant_service" / "main.py"


def _collect_src_imports(root: Path) -> list[tuple[Path, int, str]]:
    """Return list of (file, lineno, line) for every ``from src.`` /
    ``import src.`` occurrence under ``root``."""
    hits: list[tuple[Path, int, str]] = []
    for fp in sorted(root.rglob("*.py")):
        if "__pycache__" in fp.parts:
            continue
        for ln, line in enumerate(fp.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("from src.", "import src.")):
                hits.append((fp, ln, line.rstrip()))
    return hits


def test_core_tree_has_zero_src_imports() -> None:
    """core/ is hermetically sealed — no reach-back into gateway src/."""
    hits = _collect_src_imports(CORE_ROOT)
    assert not hits, (
        "Found `src.*` imports inside apps/assistant-service/src/"
        "assistant_service/core/. These violate the Phase-4 isolation "
        "contract. Use ai_gateway_core Protocols or accept via DI.\n\n"
        + "\n".join(f"  {fp.relative_to(REPO_ROOT)}:{ln}: {line}"
                    for fp, ln, line in hits)
    )


def test_main_py_is_only_src_import_site() -> None:
    """main.py is the sole composition-root allowed to import from src/."""
    hits = _collect_src_imports(APPS_ROOT / "src")
    offenders = [(fp, ln, line) for fp, ln, line in hits if fp != MAIN_PY]
    assert not offenders, (
        "Found `src.*` imports outside main.py. Composition-root imports "
        "must be confined to main.py.\n\n"
        + "\n".join(f"  {fp.relative_to(REPO_ROOT)}:{ln}: {line}"
                    for fp, ln, line in offenders)
    )
