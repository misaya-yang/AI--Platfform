"""
Tool Audit Service — audit logging and rate limiting for tool calls.

ADR-002 Phase 1: Logs every tool/skill/mcp invocation with
tenant_id, user_id, latency, and status. Provides rate limiting
queries against the audit table.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from ....core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ToolAuditEntry:
    """Single audit record for a tool invocation."""

    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    tool_type: str        # "tool" | "skill" | "mcp"
    tool_name: str
    input_summary: str    # Truncated input (max 500 chars)
    output_status: str    # "success" | "error" | "denied"
    error_message: str | None = None
    latency_ms: float = 0
    timestamp: float = 0

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = time.time()


class ToolAuditService:
    """Persist audit logs and enforce rate limits."""

    def __init__(self, database: Any) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    async def log(self, entry: ToolAuditEntry) -> None:
        """Write one audit entry. Best-effort — failures are logged but not raised."""
        if not self._database:
            return

        try:
            await self._database.execute(
                """
                INSERT INTO tool_audit_log (
                    tenant_id, user_id, session_id, request_id,
                    tool_type, tool_name, input_summary,
                    output_status, error_message, latency_ms, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, NOW())
                """,
                entry.tenant_id,
                entry.user_id,
                entry.session_id,
                entry.request_id,
                entry.tool_type,
                entry.tool_name,
                (entry.input_summary or "")[:500],
                entry.output_status,
                entry.error_message,
                entry.latency_ms,
            )
        except Exception as e:
            logger.warning(f"Audit log write failed: {e}")

    # ------------------------------------------------------------------
    # Rate Limiting
    # ------------------------------------------------------------------

    async def check_rate_limit(
        self,
        tenant_id: str,
        user_id: str,
        limit_per_minute: int = 20,
    ) -> bool:
        """Return True if the user is within rate limit, False if exceeded."""
        if not self._database or limit_per_minute <= 0:
            return True

        try:
            row = await self._database.fetchrow(
                """
                SELECT COUNT(*) AS cnt FROM tool_audit_log
                WHERE tenant_id = $1 AND user_id = $2
                  AND created_at > NOW() - INTERVAL '1 minute'
                """,
                tenant_id,
                user_id,
            )
            count = row["cnt"] if row else 0
            return count < limit_per_minute
        except Exception as e:
            logger.warning(f"Rate limit check failed: {e}")
            return True  # Fail open

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def classify_tool_type(tool_name: str) -> str:
        """Infer tool_type from tool name convention."""
        if tool_name.startswith("mcp_"):
            return "mcp"
        if tool_name.startswith("skill_"):
            return "skill"
        return "tool"

    @staticmethod
    def summarize_input(arguments: dict[str, Any] | None, max_len: int = 500) -> str:
        """Create a safe summary of tool input arguments."""
        if not arguments:
            return ""
        try:
            text = json.dumps(arguments, ensure_ascii=False, default=str)
        except Exception:
            text = str(arguments)
        return text[:max_len]
