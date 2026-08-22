"""Code-only CHR-06 contract gate; never fabricates production evidence."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    route = (ROOT / "src/api/v2/agent.py").read_text()
    assert "/threads" in route and ":interrupt" in route
    assert "V1 remains the compatibility surface" in route
    guard = (ROOT / "src/services/codex_runtime/cutover_guard.py").read_text()
    assert "CODEX_RUNTIME_LEGACY_LOOP_DELETION_BLOCKED" in guard
    assert "legacy_calls == 0" in guard
    assert "rollout_percent == 100" in guard
    assert (ROOT / "sdk/openapi.json").exists()
    print(json.dumps({"code_contract": "passed", "full_cutover": "not_verified", "legacy_loop_deletion": "not_authorized"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
