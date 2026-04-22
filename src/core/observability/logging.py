"""Backward-compat shim. Logging moved to ``ai_gateway_core.logging`` as
part of the Assistant Service True Isolation migration (phase 2). Import
from ``ai_gateway_core.logging`` directly in new code.
"""

from ai_gateway_core.logging import (
    ContextLogger,
    LogContext,
    SimpleFormatter,
    StructuredFormatter,
    clear_log_context,
    configure_structured_logging,
    get_log_context,
    get_logger,
    set_log_context,
)

__all__ = [
    "ContextLogger",
    "LogContext",
    "SimpleFormatter",
    "StructuredFormatter",
    "clear_log_context",
    "configure_structured_logging",
    "get_log_context",
    "get_logger",
    "set_log_context",
]
