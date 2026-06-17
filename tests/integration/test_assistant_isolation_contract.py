"""
Phase 0 black-box isolation contract test for the Assistant Service.

This is a regression GATE, not a benchmark. It exercises the
gateway -> assistant-service HTTP path end-to-end to prove the external
contract (response shape, streaming terminal event) does not drift during the
True-Isolation migration (plans/Assistant-Service-True-Isolation-Plan.md §3).

Requirements to actually execute (otherwise it skips cleanly):
  - Gateway reachable at GATEWAY_BASE_URL (default http://localhost:8080).
  - Optional direct assistant-service reachability if
    ASSISTANT_REQUIRE_DIRECT=true. The default compose topology keeps
    assistant-service private, so the gateway route is the release contract.
  - Credentials for a throwaway user supplied via env. If credentials are
    absent, the test skips — never fails — so CI gates without provisioned
    infra stay green.

The test DOES NOT assert rich agent behavior (no tool use, no KB retrieval).
It only pins the HTTP contract: response fields + SSE terminal envelope.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from typing import Any

import httpx
import pytest

GATEWAY_BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://localhost:8080").rstrip("/")
ASSISTANT_BASE_URL = os.getenv("ASSISTANT_BASE_URL", "http://localhost:8093").rstrip("/")
REQUIRE_ASSISTANT_DIRECT = os.getenv("ASSISTANT_REQUIRE_DIRECT", "").lower() == "true"
API_PREFIX = f"{GATEWAY_BASE_URL}/api/v1"

USER_EMAIL = os.getenv("ASSISTANT_ISOLATION_EMAIL", os.getenv("ASSISTANT_E2E_USER1_EMAIL", ""))
USER_PASSWORD = os.getenv("ASSISTANT_ISOLATION_PASSWORD", os.getenv("ASSISTANT_E2E_PASSWORD", ""))

SHORT_PROMPT = "ping"


def _reach(url: str, timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/health")
            return r.status_code < 500
    except Exception:
        return False


def _require_live_services() -> None:
    missing = []
    if not _reach(GATEWAY_BASE_URL):
        missing.append(f"gateway@{GATEWAY_BASE_URL}")
    if REQUIRE_ASSISTANT_DIRECT and not _reach(ASSISTANT_BASE_URL):
        missing.append(f"assistant@{ASSISTANT_BASE_URL}")
    if missing:
        pytest.skip(f"isolation contract skipped — unreachable: {', '.join(missing)}")
    if not USER_EMAIL or not USER_PASSWORD:
        pytest.skip(
            "isolation contract skipped — set ASSISTANT_ISOLATION_EMAIL + "
            "ASSISTANT_ISOLATION_PASSWORD (or the ASSISTANT_E2E_* equivalents)"
        )


def _login(client: httpx.Client) -> str:
    r = client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": USER_EMAIL, "password": USER_PASSWORD},
        timeout=15.0,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json().get("access_token")
    assert token, r.json()
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _resolve_model_id(client: httpx.Client, token: str) -> str:
    override = os.getenv("ASSISTANT_ISOLATION_MODEL", os.getenv("ASSISTANT_E2E_MODEL_ID", "")).strip()
    if override:
        return override

    models_resp = client.get(
        f"{API_PREFIX}/assistant/models",
        headers=_auth(token),
        timeout=15.0,
    )
    assert models_resp.status_code == 200, models_resp.text
    models = models_resp.json().get("models") or []
    assert models, (
        "no available assistant models; configure provider credentials that match "
        "enabled llm_models before running the black-box isolation contract"
    )

    config_resp = client.get(
        f"{API_PREFIX}/assistant/config",
        headers=_auth(token),
        timeout=15.0,
    )
    default_model_id = ""
    if config_resp.status_code == 200:
        default_model_id = str(config_resp.json().get("default_model_id") or "")

    model_ids = [str(m.get("id") or "") for m in models]
    return default_model_id if default_model_id in model_ids else model_ids[0]


@pytest.mark.integration
def test_assistant_isolation_contract_nonstream() -> None:
    """Non-streaming chat must return content, usage, session_id, run_id."""
    _require_live_services()

    with httpx.Client(timeout=30.0) as client:
        token = _login(client)
        model_id = _resolve_model_id(client, token)
        session_id = str(uuid.uuid4())
        r = client.post(
            f"{API_PREFIX}/assistant/chat",
            headers=_auth(token),
            json={
                "session_id": session_id,
                "message": SHORT_PROMPT,
                "model_id": model_id,
                # Keep the request cheap and deterministic.
                "temperature": 0.0,
                "max_tokens": 16,
                "kb_mode": "off",
                "web_search_enabled": False,
            },
            timeout=30.0,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # Contract pins — see apps/assistant-service/.../api/routes/chat.py::chat.
        assert "content" in body, body
        assert "usage" in body, body
        assert "session_id" in body, body
        assert body["session_id"] == session_id
        # `run_id` is part of the documented response shape; it may be None if
        # run tracking is disabled, but the KEY must exist.
        assert "run_id" in body, body


@pytest.mark.integration
def test_assistant_isolation_contract_stream() -> None:
    """Streaming chat must emit >=1 text-delta-ish event and a terminal event."""
    _require_live_services()

    with httpx.Client(timeout=30.0) as client:
        token = _login(client)
        model_id = _resolve_model_id(client, token)
        session_id = str(uuid.uuid4())
        events: list[dict[str, Any]] = []
        terminal_seen = False
        # Text-delta-ish events the current implementation emits.
        delta_event_types = {"text_delta", "tool_call_result", "content"}
        terminal_event_types = {"done", "run_finished", "run_error", "error"}

        with client.stream(
            "POST",
            f"{API_PREFIX}/assistant/chat/stream",
            headers=_auth(token),
            json={
                "session_id": session_id,
                "message": SHORT_PROMPT,
                "model_id": model_id,
                "temperature": 0.0,
                "max_tokens": 16,
                "kb_mode": "off",
                "web_search_enabled": False,
            },
            timeout=45.0,
        ) as resp:
            assert resp.status_code == 200, resp.text
            # Gateway is expected to echo session_id either in JSON or headers.
            header_session = resp.headers.get("X-Session-Id")
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if not raw:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    evt = json.loads(raw)
                    events.append(evt)
                    if evt.get("event_type") in terminal_event_types:
                        terminal_seen = True
                        break

        assert events, "no SSE events received"
        seen_types = {e.get("event_type") for e in events}
        assert seen_types & delta_event_types, (
            f"no delta-class event observed; saw: {seen_types}"
        )
        assert terminal_seen, f"no terminal event in stream; saw: {seen_types}"

        # session_id / run_id observability: either the stream carries a
        # run_started event with both, or the gateway returns them in headers.
        # We assert at least one of those paths holds so downstream correlation
        # doesn't silently regress.
        run_started = next(
            (e for e in events if e.get("event_type") == "run_started"), None
        )
        has_run_id = False
        has_session_id = False
        if run_started:
            data = run_started.get("data") or {}
            has_run_id = bool(data.get("run_id"))
            has_session_id = bool(data.get("session_id")) or session_id == data.get("session_id")
        if not has_session_id:
            has_session_id = header_session == session_id
        assert has_session_id, "stream did not expose session_id (neither event nor X-Session-Id header)"
        # run_id presence is softer — only assert when the implementation
        # actually emits run_started, so this contract doesn't over-fit.
        if run_started is not None:
            assert has_run_id, f"run_started event missing run_id: {run_started}"
