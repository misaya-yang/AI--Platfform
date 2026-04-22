"""
Snapshot the assistant-service OpenAPI spec in-process to a deterministic JSON file.

Used by Phase 0 of the Assistant Service True Isolation Migration to produce a
baseline spec, and by later phases to verify the extraction did not drift the
external contract.

Usage (from repo root):
    uv run python scripts/snapshot_assistant_openapi.py
    # → writes tests/fixtures/assistant_openapi_baseline.json

The assistant-service `main.py` carries a sys.path shim that makes `src.*`
importable when launched from the repo root. This script assumes the same:
it must be run with `REPO_ROOT` as the working directory so the shim lines up.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSISTANT_SRC = REPO_ROOT / "apps" / "assistant-service" / "src"

# Put both gateway `src.*` importability (already handled by the main.py shim,
# but harmless to make explicit) and assistant-service `assistant_service.*`
# on sys.path so we can import the FastAPI app in-process.
for p in (str(REPO_ROOT), str(ASSISTANT_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    # Import AFTER sys.path setup. The assistant-service app is constructed at
    # module import time; we don't trigger the lifespan (that would need a DB,
    # redis, provider keys, etc.), because FastAPI.openapi() only reads routes
    # and schemas — it doesn't run startup.
    from assistant_service.main import app  # noqa: E402

    spec = app.openapi()

    out_path = REPO_ROOT / "tests" / "fixtures" / "assistant_openapi_baseline.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, sort_keys=True)
        f.write("\n")

    path_count = len(spec.get("paths", {}))
    print(f"Wrote OpenAPI baseline to {out_path} ({path_count} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
