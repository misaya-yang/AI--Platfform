"""Trusted composition helpers for the canonical tool invocation path."""

from __future__ import annotations

from typing import Any

from ..tool_invocation_contracts import ToolInvoker
from .tool_audit import ToolAuditService


def create_audited_tool_invoker(
    *,
    database: Any,
    tool_audit: Any | None = None,
    **invoker_dependencies: Any,
) -> ToolInvoker:
    """Build the existing invoker with its process-scoped durable audit backend."""

    from ..tool_invoker import create_tool_invoker

    audit_backend = tool_audit if tool_audit is not None else ToolAuditService(database)
    return create_tool_invoker(
        tool_audit=audit_backend,
        **invoker_dependencies,
    )
