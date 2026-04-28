from __future__ import annotations

import logging
import os

# This service's Dockerfile context is ``apps/islamic-content-service``,
# which does NOT include the workspace package ``packages/ai-gateway-core``.
# Importing from it would crash the container at startup. Until the
# Dockerfile is reworked to use repo-root build context (followup), we
# degrade gracefully to stdlib logging when the bridge isn't available.
try:
    from ai_gateway_core.logging import configure_structured_logging  # type: ignore[import-not-found]
    _HAS_BRIDGE = True
except ImportError:
    _HAS_BRIDGE = False


def configure_logging(level: str = "INFO") -> None:
    """Configure logging for the Islamic Content Service.

    Preferred path: route through ``ai_gateway_core.logging`` so every
    record carries request_id (from REQUEST_ID_CTX), trace_id / span_id
    (when an OTel span is active), and ``service``. Format:
    LOG_FORMAT env > ENVIRONMENT=production → json, else simple.

    Fallback path: when ``ai_gateway_core`` isn't on the path (this
    service's Dockerfile doesn't yet install it), use stdlib logging.
    Records carry no request_id / trace_id but the service stays up.
    """
    log_format = os.environ.get("LOG_FORMAT")
    if not log_format:
        log_format = (
            "json"
            if os.environ.get("ENVIRONMENT", "").lower() == "production"
            else "simple"
        )
    if _HAS_BRIDGE:
        configure_structured_logging(
            level=level,
            format_type=log_format,
            service="islamic-content-service",
            log_to_file=False,
        )
        return
    # stdlib fallback — flat, no structured fields, but the service runs.
    fmt = (
        '{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s",'
        '"service":"islamic-content-service"}'
        if log_format == "json"
        else "%(asctime)s %(levelname)s %(name)s - %(message)s"
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
    )
    logging.getLogger(__name__).warning(
        "ai_gateway_core not available — falling back to stdlib logging "
        "(no request_id/trace_id correlation). Fix: update Dockerfile to "
        "install workspace package."
    )
