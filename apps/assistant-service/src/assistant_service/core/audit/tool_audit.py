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

from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import SENSITIVE_KEY_RE, redact_trace_text

logger = get_logger(__name__)

_AUDIT_TEXT_LIMIT = 500
_TRUNCATION_SUFFIX = "...[truncated]"


def _bounded_redacted_text(value: Any, *, limit: int = _AUDIT_TEXT_LIMIT) -> str:
    """Return secret-redacted audit text with a hard character bound."""

    if limit <= 0:
        return ""
    try:
        text = redact_trace_text(value)
    except Exception:
        return "[redacted]"[:limit]
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATION_SUFFIX):
        return _TRUNCATION_SUFFIX[:limit]
    return f"{text[: limit - len(_TRUNCATION_SUFFIX)]}{_TRUNCATION_SUFFIX}"


@dataclass
class ToolAuditEntry:
    """Single audit record for a tool invocation."""

    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    tool_type: str  # "tool" | "skill" | "mcp"
    tool_name: str
    input_summary: str  # Truncated input (max 500 chars)
    output_status: str  # "success" | "error" | "denied"
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
                _bounded_redacted_text(entry.input_summary or ""),
                entry.output_status,
                (
                    _bounded_redacted_text(entry.error_message)
                    if entry.error_message is not None
                    else None
                ),
                entry.latency_ms,
            )
        except Exception as exc:
            logger.warning(
                "tool_audit.write_failed (exception_type=%s)",
                type(exc).__name__,
            )

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
        except Exception as exc:
            logger.warning(
                "tool_audit.rate_limit_check_failed (exception_type=%s)",
                type(exc).__name__,
            )
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
        safe_arguments = ToolAuditService._redact_arguments(arguments)
        try:
            text = json.dumps(safe_arguments, ensure_ascii=False, default=str)
        except Exception:
            text = str(safe_arguments)
        return _bounded_redacted_text(text, limit=max_len)

    @staticmethod
    def _redact_arguments(value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in list(value.items())[:100]:
                key_text = _bounded_redacted_text(key, limit=100)
                if SENSITIVE_KEY_RE.search(key_text):
                    safe[key_text] = "[redacted]"
                else:
                    safe[key_text] = ToolAuditService._redact_arguments(item)
            return safe
        if isinstance(value, list):
            return [ToolAuditService._redact_arguments(item) for item in value[:100]]
        if isinstance(value, str):
            return _bounded_redacted_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return _bounded_redacted_text(value)
