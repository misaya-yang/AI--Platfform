"""Code-only CHR-05 contract gate.

This intentionally does not claim a canary window, Docker acceptance, or
provider quality cohort. Deployment evidence remains an explicit release gate.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    migration = (ROOT / "database/migrations/092_codex_runtime_legacy_import.sql").read_text()
    assert "import_assistant_legacy_session" in migration
    assert "ASSISTANT_RUNTIME_IMPORT_IN_FLIGHT" in migration
    assert "FOR UPDATE" in migration
    assert "assistant_runtime_thread_projections" in migration
    normalization = (
        ROOT / "database/migrations/094_codex_runtime_legacy_import_normalization.sql"
    ).read_text()
    assert "to_regclass('assistant.sessions')" in normalization
    assert "ASSISTANT_RUNTIME_IMPORT_TOOL_PAIRING_INVALID" in normalization
    assert "codex-runtime-legacy-approval/v1" in normalization
    routes = (ROOT / "src/api/v2/agent.py").read_text()
    assert "/threads/{thread_id}/events" in routes
    assert "after_sequence" in routes
    policy = (ROOT / "src/services/assistant_runtime_assignment.py").read_text()
    assert "ASSISTANT_RUNTIME_CANARY_PERCENT" in policy
    assert "ASSISTANT_RUNTIME_CANARY_KILL_SWITCH" in policy
    assert "tenant_id}:{session_id}" in policy
    v1 = (ROOT / "src/api/v1/assistant.py").read_text()
    assert "bind_new_session" in v1
    print(json.dumps({"code_contract": "passed", "production_canary": "not_verified"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
