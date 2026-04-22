"""
Phase 0 OpenAPI contract test for the Assistant Service.

Verifies the current assistant-service OpenAPI spec (fetched from a live
service or produced in-process) is a SUPERSET of the committed baseline at
tests/fixtures/assistant_openapi_baseline.json.

"Superset" means:
  - Every path that was in the baseline is still present.
  - For every (path, method) pair in the baseline, all required request-body
    fields in the baseline are still required in the current spec.

Removing a path or turning a required field into an optional one is treated
as a breaking change and fails the test.

The test skips cleanly if:
  - A live assistant-service is unreachable, AND
  - The in-process import of `assistant_service.main:app` also fails.

Refresh the baseline (intentionally) with:
    uv run python scripts/snapshot_assistant_openapi.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

BASELINE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "assistant_openapi_baseline.json"
)
ASSISTANT_BASE_URL = os.getenv("ASSISTANT_BASE_URL", "http://localhost:8093").rstrip("/")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ASSISTANT_SRC = REPO_ROOT / "apps" / "assistant-service" / "src"


def _load_live_spec() -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=2.0) as c:
            r = c.get(f"{ASSISTANT_BASE_URL}/openapi.json")
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


def _load_inprocess_spec() -> dict[str, Any] | None:
    for p in (str(REPO_ROOT), str(ASSISTANT_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from assistant_service.main import app  # type: ignore[import-not-found]

        return app.openapi()
    except Exception:
        return None


def _load_current_spec() -> dict[str, Any]:
    spec = _load_live_spec() or _load_inprocess_spec()
    if spec is None:
        pytest.skip(
            "assistant OpenAPI spec unavailable — service not reachable and "
            "in-process import failed"
        )
    return spec


def _required_for(spec: dict[str, Any], path: str, method: str) -> set[str]:
    op = spec.get("paths", {}).get(path, {}).get(method.lower()) or {}
    req = op.get("requestBody") or {}
    content = req.get("content") or {}
    json_schema = (content.get("application/json") or {}).get("schema") or {}
    # Resolve a single-level $ref so we can see the actual model.
    ref = json_schema.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.split("/")[-1]
        json_schema = (
            spec.get("components", {}).get("schemas", {}).get(name) or json_schema
        )
    return set(json_schema.get("required") or [])


@pytest.mark.integration
def test_assistant_openapi_superset_of_baseline() -> None:
    if not BASELINE_PATH.exists():
        pytest.skip(f"baseline missing: {BASELINE_PATH}")
    baseline = json.loads(BASELINE_PATH.read_text())
    current = _load_current_spec()

    baseline_paths = set(baseline.get("paths", {}).keys())
    current_paths = set(current.get("paths", {}).keys())
    removed = baseline_paths - current_paths
    assert not removed, f"OpenAPI drift: paths removed vs baseline: {sorted(removed)}"

    # For every (path, method) in baseline, required fields must not shrink.
    drifted: list[str] = []
    for path, ops in baseline.get("paths", {}).items():
        for method in ops:
            if method.startswith("x-"):
                continue
            base_required = _required_for(baseline, path, method)
            cur_required = _required_for(current, path, method)
            lost = base_required - cur_required
            if lost:
                drifted.append(f"{method.upper()} {path}: lost required {sorted(lost)}")
    assert not drifted, "OpenAPI drift: required fields weakened:\n  " + "\n  ".join(drifted)
