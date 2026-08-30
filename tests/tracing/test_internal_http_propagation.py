from __future__ import annotations

import pytest
from ai_gateway_core.proxy.request_id_middleware import REQUEST_ID_CTX
from ai_gateway_core.tracing import internal_http_headers


def test_internal_headers_propagate_trace_and_bounded_correlation(monkeypatch) -> None:
    def inject(carrier: dict[str, str]) -> None:
        carrier["traceparent"] = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
        carrier["tracestate"] = "vendor=value"

    monkeypatch.setattr("opentelemetry.propagate.inject", inject)
    token = REQUEST_ID_CTX.set("request-a")
    try:
        headers = internal_http_headers(
            {"x-service-auth": "opaque"},
            run_id="run-a",
            turn_id="turn-a",
            execution_id="execution-a",
        )
    finally:
        REQUEST_ID_CTX.reset(token)

    assert headers == {
        "x-service-auth": "opaque",
        "x-request-id": "request-a",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "vendor=value",
        "x-ai-run-id": "run-a",
        "x-ai-turn-id": "turn-a",
        "x-ai-execution-id": "execution-a",
    }


def test_internal_headers_reject_log_injection_in_correlation_ids() -> None:
    with pytest.raises(ValueError, match="malformed"):
        internal_http_headers(run_id="run-a\nsecret=value")
