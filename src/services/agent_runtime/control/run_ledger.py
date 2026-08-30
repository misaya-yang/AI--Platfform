"""Terminal run ledger writes: lease revocation and assistant_runs status.

ARC-02 split of ``control_plane.py``.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..control_plane import AgentRuntimeControlPlane


async def complete_run(
    plane: AgentRuntimeControlPlane, run_id: uuid.UUID, terminal_status: str
) -> None:
    status = (
        "succeeded"
        if terminal_status == "succeeded"
        else "cancelled"
        if terminal_status == "cancelled"
        else "failed"
    )
    usage = await plane.database.fetchrow(
        """
        SELECT COALESCE(SUM(input_tokens), 0)::bigint AS input_tokens,
               COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
               COALESCE(SUM(cost_microusd), 0)::bigint AS cost_microusd
          FROM assistant_runtime_model_calls
         WHERE run_id = $1 AND status = 'completed'
        """,
        run_id,
    )
    await plane.database.execute(
        """
        UPDATE assistant_runtime_model_leases
           SET status = 'revoked', revoked_at = NOW(),
               revoked_reason = $2, updated_at = NOW()
         WHERE run_id = $1 AND status = 'active'
        """,
        run_id,
        f"turn_{status}",
    )
    await plane.database.execute(
        """
        UPDATE assistant_runs
           SET status = $2, usage = $3::jsonb, finished_at = NOW(), updated_at = NOW()
         WHERE run_id = $1 AND engine = 'agent_runtime' AND status = 'running'
        """,
        run_id,
        status,
        json.dumps(dict(usage or {}), separators=(",", ":")),
    )


async def fail_run(
    plane: AgentRuntimeControlPlane,
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    reason: str,
) -> None:
    await plane.database.execute(
        """
        UPDATE assistant_runtime_model_leases
           SET status = 'revoked', revoked_at = NOW(), revoked_reason = $3, updated_at = NOW()
         WHERE run_id = $1 AND snapshot_id = $2 AND status = 'active'
        """,
        run_id,
        snapshot_id,
        reason,
    )
    await plane.database.execute(
        """
        UPDATE assistant_runs
           SET status = 'failed', error = $2, finished_at = NOW(), updated_at = NOW()
         WHERE run_id = $1 AND engine = 'agent_runtime' AND status = 'running'
        """,
        run_id,
        reason,
    )


__all__ = ["complete_run", "fail_run"]
