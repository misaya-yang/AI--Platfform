"""GATE-ADR004-1 + -6 — silent-regression detector for in-memory reads.

Every ``self._{runs,approvals,commands}.(get|values|items)`` access in
``apps/assistant-service/.../core/gateway/execution_gateway.py`` must
be tagged ``# AUDIT-OK`` with an explicit justification (DB-less
fallback / write-through mirror). Untagged reads regress the ADR-004
decision to "DB is authoritative" and would reintroduce the
cross-instance split-brain that blocks Polaris items #4 and #5.

This test guards the invariant so a future refactor can't silently
re-introduce an in-memory read.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

EXEC_GW_PATH = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "assistant-service"
    / "src"
    / "assistant_service"
    / "core"
    / "gateway"
    / "execution_gateway.py"
)

PATTERN = re.compile(r"self\._(runs|approvals|commands)\.(get|values|items)")


def test_every_in_memory_read_is_audit_ok_tagged():
    assert EXEC_GW_PATH.exists(), f"missing file: {EXEC_GW_PATH}"
    unauthorized: list[tuple[int, str]] = []
    for lineno, line in enumerate(EXEC_GW_PATH.read_text().splitlines(), start=1):
        if PATTERN.search(line) and "# AUDIT-OK" not in line:
            unauthorized.append((lineno, line.strip()))
    assert not unauthorized, (
        "ADR-004 silent regression: untagged in-memory reads in execution_gateway.py:\n"
        + "\n".join(f"  L{ln}: {t}" for ln, t in unauthorized)
        + "\n\nEach untagged read must be either:\n"
        "  (a) replaced with a DB SELECT, OR\n"
        "  (b) tagged `# AUDIT-OK: <justification>` — allowed justifications:\n"
        "      - 'write-through mirror' (the line mutates, does not branch on read)\n"
        "      - 'DB-less / DB-error fallback only' (runs after a successful DB path)\n"
    )


def test_require_db_env_blocks_in_memory_only_construction(monkeypatch):
    """GATE-ADR004-3 — when ASSISTANT_REQUIRE_DB=true, constructing the
    gateway without a ``database`` must raise. Production sets this
    flag; dev/test do not.
    """
    import pytest
    from assistant_service.core.gateway.execution_gateway import (
        AssistantExecutionGateway,
    )

    monkeypatch.setenv("ASSISTANT_REQUIRE_DB", "true")
    with pytest.raises(RuntimeError, match="ASSISTANT_REQUIRE_DB"):
        AssistantExecutionGateway(
            tool_invoker=object(),  # shape-compatible stub — __init__ only stores it
            database=None,
        )


def test_require_db_env_unset_allows_in_memory_construction(monkeypatch):
    """Dev / test path must not break because of the ADR guard when
    the env flag is not set."""
    from assistant_service.core.gateway.execution_gateway import (
        AssistantExecutionGateway,
    )

    monkeypatch.delenv("ASSISTANT_REQUIRE_DB", raising=False)
    # Should not raise.
    gw = AssistantExecutionGateway(tool_invoker=object(), database=None)
    assert gw.database is None


async def test_start_run_does_not_update_memory_when_db_owner_gate_rejects(
    monkeypatch,
):
    """DB path must reject cross-owner upserts before changing the fallback mirror."""
    import pytest
    from assistant_service.core.gateway.execution_gateway import (
        AssistantExecutionGateway,
        RunRecord,
    )

    class RejectingDatabase:
        async def fetchrow(self, _query: str, *_args: Any) -> None:
            return None

    monkeypatch.delenv("ASSISTANT_REQUIRE_DB", raising=False)
    gw = AssistantExecutionGateway(tool_invoker=object(), database=RejectingDatabase())
    gw._runs["run-1"] = RunRecord(
        run_id="run-1",
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        status="running",
        engine="gemini",
        execution_profile="standard",
        memory_mode="auto",
        os_agent_enabled=False,
        request_preview="original",
    )

    with pytest.raises(PermissionError, match="different owner"):
        await gw.start_run(
            run_id="run-1",
            tenant_id="tenant-b",
            user_id="user-b",
            session_id="session-b",
            engine="gemini",
            execution_profile="standard",
            memory_mode="auto",
            os_agent_enabled=False,
            request_preview="new",
        )

    assert gw._runs["run-1"].tenant_id == "tenant-a"
    assert gw._runs["run-1"].user_id == "user-a"
    assert gw._runs["run-1"].request_preview == "original"


def test_audit_ok_justifications_are_from_the_allowed_set():
    """AUDIT-OK comments must use one of the approved reasons — not
    ad-hoc "this is fine" hand-waves.
    """
    allowed = {
        "write-through mirror",
        "write-through mirror, not a read source",
        "DB-less / DB-error fallback only",
    }
    suspicious: list[tuple[int, str]] = []
    for lineno, line in enumerate(EXEC_GW_PATH.read_text().splitlines(), start=1):
        if "# AUDIT-OK" not in line:
            continue
        if not PATTERN.search(line):
            continue
        # Extract the justification text after "AUDIT-OK:"
        marker = line.find("# AUDIT-OK")
        justification = line[marker:].replace("# AUDIT-OK:", "").strip()
        if not any(j in justification for j in allowed):
            suspicious.append((lineno, justification))
    assert not suspicious, (
        "AUDIT-OK justifications outside the approved set:\n"
        + "\n".join(f"  L{ln}: {j!r}" for ln, j in suspicious)
        + f"\n\nApproved set: {sorted(allowed)}"
    )
