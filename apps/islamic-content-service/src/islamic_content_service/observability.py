from __future__ import annotations

import os

from ai_gateway_core.logging import configure_structured_logging


def configure_logging(level: str = "INFO") -> None:
    """Configure structured logging for the Islamic Content Service.

    PR-3: routes through the shared ai_gateway_core.logging bridge so
    every record carries request_id (from REQUEST_ID_CTX), trace_id /
    span_id (when an OTel span is active), and ``service``. Format:
    LOG_FORMAT env > ENVIRONMENT=production → json, else simple.
    """
    log_format = os.environ.get("LOG_FORMAT")
    if not log_format:
        log_format = (
            "json"
            if os.environ.get("ENVIRONMENT", "").lower() == "production"
            else "simple"
        )
    configure_structured_logging(
        level=level,
        format_type=log_format,
        service="islamic-content-service",
        log_to_file=False,
    )
