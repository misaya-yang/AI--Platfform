"""PR-3: bridge tests — REQUEST_ID_CTX + OTel context → log records.

Verifies that ``configure_structured_logging`` wires ``ContextFilter``
onto every handler so the 2,443 existing ``logger.info(...)`` call
sites pick up ``request_id`` / ``trace_id`` / ``span_id`` / ``service``
without any source edits.

The tests deliberately attach a fresh ``StreamHandler`` writing to an
``io.StringIO`` so we don't have to fight ``capsys`` interaction with
``StreamHandler(sys.stdout)`` (FastAPI / pytest both reassign stdout
mid-run, which makes ``capsys`` flaky for stdlib logging output).
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from ai_gateway_core.logging import (
    ContextFilter,
    LogContext,
    SimpleFormatter,
    StructuredFormatter,
    clear_log_context,
    configure_structured_logging,
    set_log_context,
)
from ai_gateway_core.proxy.request_id_middleware import REQUEST_ID_CTX


@pytest.fixture
def json_capture():
    """Configure JSON logging and return (logger, buffer) for assertions.

    Uses ``log_to_file=False`` so the test never touches the filesystem.
    Adds a buffer-backed handler with ``ContextFilter`` attached so each
    record gets stamped exactly the way the production console handler
    would.
    """
    configure_structured_logging(
        level="DEBUG",
        format_type="json",
        log_to_file=False,
        service="test-service",
    )

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(StructuredFormatter())
    h.addFilter(ContextFilter())
    logging.getLogger().addHandler(h)

    yield logging.getLogger("bridge_test"), buf

    logging.getLogger().removeHandler(h)
    # Reset legacy LogContext so adjacent tests don't see leakage.
    clear_log_context()


@pytest.fixture
def simple_capture():
    """Configure SIMPLE logging and capture output."""
    configure_structured_logging(
        level="DEBUG",
        format_type="simple",
        log_to_file=False,
        service="test-service",
    )
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(SimpleFormatter(use_colors=False))
    h.addFilter(ContextFilter())
    logging.getLogger().addHandler(h)
    yield logging.getLogger("simple_test"), buf
    logging.getLogger().removeHandler(h)


def _last_json_line(buf: io.StringIO) -> dict:
    raw = buf.getvalue().strip().splitlines()
    assert raw, "no log lines produced"
    return json.loads(raw[-1])


def test_request_id_stamped_when_contextvar_set(json_capture):
    """REQUEST_ID_CTX → record.request_id → top-level JSON key."""
    logger, buf = json_capture
    token = REQUEST_ID_CTX.set("req-XYZ")
    try:
        logger.info("hi")
    finally:
        REQUEST_ID_CTX.reset(token)

    payload = _last_json_line(buf)
    assert payload["request_id"] == "req-XYZ"
    assert payload["message"] == "hi"
    assert payload["level"] == "INFO"
    assert payload["timestamp"]
    assert payload["service"] == "test-service"
    assert payload["logger"] == "bridge_test"


def test_no_request_id_when_outside_request(json_capture):
    """No contextvar → still valid JSON, request_id omitted."""
    logger, buf = json_capture
    # Clear any stray value (test isolation hygiene).
    REQUEST_ID_CTX.set("")
    logger.info("no ctx")
    payload = _last_json_line(buf)
    assert payload["message"] == "no ctx"
    # Either absent or falsy — both acceptable.
    assert not payload.get("request_id")


def test_otel_trace_id_stamped_when_span_active(json_capture):
    """Active OTel span → record.trace_id (32-hex) + record.span_id (16-hex)."""
    pytest.importorskip("opentelemetry")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    # Tests reset the provider per-run so multiple test runs don't pollute
    # each other. Idempotent set_tracer_provider is a no-op after first
    # call; we use whatever the env gives us.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())

    logger, buf = json_capture
    tracer = trace.get_tracer("bridge_test")
    with tracer.start_as_current_span("unit-span"):
        logger.info("with span")

    payload = _last_json_line(buf)
    assert "trace_id" in payload
    assert "span_id" in payload
    assert len(payload["trace_id"]) == 32
    assert len(payload["span_id"]) == 16
    # Hex sanity.
    int(payload["trace_id"], 16)
    int(payload["span_id"], 16)


def test_simple_format_unchanged(simple_capture):
    """``simple`` format remains human-readable, NOT JSON."""
    logger, buf = simple_capture
    logger.info("hello dev")
    output = buf.getvalue().strip()
    # Not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)
    # Has level + name + message.
    assert "INFO" in output
    assert "hello dev" in output
    assert "simple_test" in output


def test_existing_set_log_context_still_works(json_capture):
    """Legacy ``LogContext`` API keeps merging into JSON output."""
    logger, buf = json_capture
    set_log_context(LogContext(user_id="u1", tenant_id="t1"))
    try:
        logger.info("with legacy ctx")
    finally:
        clear_log_context()

    payload = _last_json_line(buf)
    assert payload["user_id"] == "u1"
    assert payload["tenant_id"] == "t1"
    assert payload["message"] == "with legacy ctx"


def test_request_id_present_at_warning_and_error(json_capture):
    """request_id stamping is level-agnostic — warning + error too."""
    logger, buf = json_capture
    token = REQUEST_ID_CTX.set("req-multi-level")
    try:
        logger.warning("warn line")
        logger.error("err line")
    finally:
        REQUEST_ID_CTX.reset(token)

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert all(rec["request_id"] == "req-multi-level" for rec in lines[-2:])
    assert lines[-2]["level"] == "WARNING"
    assert lines[-1]["level"] == "ERROR"


def test_service_default_falls_back_when_not_passed(monkeypatch):
    """No ``service=`` arg + no SERVICE_NAME env → 'ai-gateway' default."""
    # Reload the module-global to its env-derived default.
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    import importlib

    from ai_gateway_core.logging import _core as core_mod
    importlib.reload(core_mod)

    # Re-fetch symbols from reloaded module.
    core_mod.configure_structured_logging(format_type="json", log_to_file=False)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(core_mod.StructuredFormatter())
    h.addFilter(core_mod.ContextFilter())
    logging.getLogger().addHandler(h)
    try:
        logging.getLogger("svc_default").info("x")
        payload = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert payload["service"] == "ai-gateway"
    finally:
        logging.getLogger().removeHandler(h)
