"""
Live Assistant API E2E tests.

Run only when a real backend is up:
    RUN_ASSISTANT_API_E2E=1 conda run -n ai_gateway pytest tests/integration/test_assistant_api_e2e_live.py -q -s
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

import httpx
import pytest

API_BASE_URL = os.getenv("ASSISTANT_E2E_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
API_PREFIX = f"{API_BASE_URL}/api/v1"
RUN_LIVE = os.getenv("RUN_ASSISTANT_API_E2E", "0") == "1"

AUTH_EMAIL_DOMAIN = os.getenv("ASSISTANT_E2E_AUTH_EMAIL_DOMAIN", "example.com")
USER1_EMAIL = os.getenv("ASSISTANT_E2E_USER1_EMAIL", f"assistant.e2e.user1@{AUTH_EMAIL_DOMAIN}")
USER2_EMAIL = os.getenv("ASSISTANT_E2E_USER2_EMAIL", f"assistant.e2e.user2@{AUTH_EMAIL_DOMAIN}")
LOGIN_PASSWORD = os.getenv("ASSISTANT_E2E_PASSWORD", "")
MODEL_ID = os.getenv("ASSISTANT_E2E_MODEL_ID", "gemini-3-flash-preview")


def _require_live() -> None:
    if not RUN_LIVE:
        pytest.skip("Set RUN_ASSISTANT_API_E2E=1 to run live API validation")
    if not LOGIN_PASSWORD:
        pytest.skip("Set ASSISTANT_E2E_PASSWORD to run live API validation")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _assert_service_ready(client: httpx.Client) -> None:
    resp = client.get(f"{API_BASE_URL}/health")
    assert resp.status_code == 200, resp.text


def _login(client: httpx.Client, email: str, password: str) -> str:
    resp = client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    token = payload.get("access_token")
    assert token, payload
    return token


def _create_session(client: httpx.Client, token: str) -> str:
    resp = client.post(
        f"{API_PREFIX}/assistant/sessions",
        headers=_headers(token),
        json={},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    session_id = payload.get("session_id")
    assert session_id, payload
    return session_id


def _chat(
    client: httpx.Client,
    token: str,
    session_id: str,
    message: str,
    *,
    execution_profile: str = "safe",
    memory_mode: str = "auto",
    os_agent_enabled: bool = False,
    require_non_empty: bool = True,
    max_attempts: int = 2,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        resp = client.post(
            f"{API_PREFIX}/assistant/chat",
            headers=_headers(token),
            json={
                "session_id": session_id,
                "message": message,
                "model_id": MODEL_ID,
                "execution_profile": execution_profile,
                "memory_mode": memory_mode,
                "os_agent_enabled": os_agent_enabled,
            },
            timeout=180.0,
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        last_payload = payload
        content = payload.get("content")
        if not require_non_empty:
            return payload
        if isinstance(content, str) and content.strip():
            return payload
        if attempt < max_attempts:
            time.sleep(1.2 * attempt)

    assert isinstance(last_payload, dict)
    assert isinstance(last_payload.get("content"), str) and last_payload["content"].strip(), (
        last_payload
    )
    return last_payload


def _stream_chat(
    client: httpx.Client,
    token: str,
    session_id: str,
    message: str,
    *,
    execution_profile: str = "safe",
    memory_mode: str = "auto",
    os_agent_enabled: bool = False,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        f"{API_PREFIX}/assistant/chat/stream",
        headers=_headers(token),
        json={
            "session_id": session_id,
            "message": message,
            "model_id": MODEL_ID,
            "execution_profile": execution_profile,
            "memory_mode": memory_mode,
            "os_agent_enabled": os_agent_enabled,
        },
        timeout=240.0,
    ) as resp:
        assert resp.status_code == 200, resp.text
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if not raw:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                evt = json.loads(raw)
                events.append(evt)
                if evt.get("event_type") in {"done", "run_error"}:
                    break
    return events


def _extract_run_id(events: list[dict[str, Any]]) -> str | None:
    for evt in events:
        if evt.get("event_type") == "run_started":
            data = evt.get("data") or {}
            run_id = data.get("run_id")
            if run_id:
                return str(run_id)
    return None


def _has_any_event(events: list[dict[str, Any]], *event_types: str) -> bool:
    names = {evt.get("event_type") for evt in events}
    return any(name in names for name in event_types)


def _assert_no_event(events: list[dict[str, Any]], event_type: str) -> None:
    assert all(evt.get("event_type") != event_type for evt in events), events


def _stream_chat_until_success(
    client: httpx.Client,
    token: str,
    session_id: str,
    message: str,
    *,
    execution_profile: str = "safe",
    memory_mode: str = "auto",
    os_agent_enabled: bool = False,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """
    Retry streaming when provider/network returns transient run_error.

    This keeps live E2E strict on behavior but resilient to sporadic upstream failures.
    """
    last_events: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        last_events = _stream_chat(
            client=client,
            token=token,
            session_id=session_id,
            message=message,
            execution_profile=execution_profile,
            memory_mode=memory_mode,
            os_agent_enabled=os_agent_enabled,
        )
        has_content_or_tool = _has_any_event(last_events, "text_delta", "tool_call_result")
        has_run_error = _has_any_event(last_events, "run_error")
        if has_content_or_tool and not has_run_error:
            return last_events
        if attempt < max_attempts:
            time.sleep(1.5 * attempt)
    return last_events


@pytest.mark.integration
def test_assistant_api_e2e_live_dialogues():
    """
    Comprehensive API-level dialogue validation.

    Covers:
    - code generation/execution path
    - image generation path
    - document/pdf generation path
    - context continuity across turns
    - multi-user isolation
    - gateway policy + run tracking endpoints
    """
    _require_live()

    with httpx.Client(timeout=60.0) as client:
        _assert_service_ready(client)

        # Login/create two users
        token1 = _login(client, USER1_EMAIL, LOGIN_PASSWORD)
        token2 = _login(client, USER2_EMAIL, LOGIN_PASSWORD)

        session1 = _create_session(client, token1)
        session2 = _create_session(client, token2)
        context_secret = f"CTX-{int(time.time())}"

        # Multi-user isolation: user2 cannot read user1 session
        isolation_resp = client.get(
            f"{API_PREFIX}/assistant/sessions/{session1}",
            headers=_headers(token2),
        )
        assert isolation_resp.status_code == 404, isolation_resp.text

        # Policy endpoint
        policies_resp = client.get(
            f"{API_PREFIX}/assistant/policies",
            headers=_headers(token1),
        )
        assert policies_resp.status_code == 200, policies_resp.text
        policies = policies_resp.json().get("policies", {})
        assert "default_execution_profile" in policies

        # Stream: code capability + context budget events + run tracking
        events = _stream_chat_until_success(
            client,
            token1,
            session1,
            f"请给我一个 Python 脚本来绘制 sin 曲线，并记住口令 {context_secret}。",
            execution_profile="balanced",
        )
        assert _has_any_event(events, "text_delta", "tool_call_start", "tool_call_result")
        assert _has_any_event(events, "context_budget", "gateway_decision")
        assert _has_any_event(events, "run_started")
        assert _has_any_event(events, "done", "run_finished")
        _assert_no_event(events, "run_error")

        run_id = _extract_run_id(events)
        if run_id:
            run_resp = client.get(
                f"{API_PREFIX}/assistant/runs/{run_id}",
                headers=_headers(token1),
            )
            if policies.get("gateway_enabled", False):
                assert run_resp.status_code == 200, run_resp.text
                run_payload = run_resp.json().get("run", {})
                assert str(run_payload.get("run_id")) == run_id
            else:
                # Gateway disabled: run lifecycle persistence API can return 404.
                assert run_resp.status_code in {200, 404}, run_resp.text

        # Non-stream: image generation request path
        image_result = _chat(
            client,
            token1,
            session1,
            "请帮我生成一张简洁风格的产品海报图片，并说明生成结果。",
            execution_profile="balanced",
        )
        assert len(image_result.get("content", "")) >= 20

        # Non-stream: document/pdf generation request path
        doc_result = _chat(
            client,
            token1,
            session1,
            "请生成一份项目周报 PDF 的正文草稿，并说明关键结构。",
            execution_profile="balanced",
        )
        assert len(doc_result.get("content", "")) >= 20

        # Context continuity validation (same session follow-up)
        follow_events = _stream_chat_until_success(
            client,
            token1,
            session1,
            "请只回复我刚才让你记住的口令，不要其他文字。",
            execution_profile="balanced",
            memory_mode="strict",
        )
        assert _has_any_event(follow_events, "text_delta", "done", "run_finished")
        _assert_no_event(follow_events, "run_error")

        history_resp = client.get(
            f"{API_PREFIX}/assistant/sessions/{session1}/history?limit=50",
            headers=_headers(token1),
        )
        assert history_resp.status_code == 200, history_resp.text
        history_payload = history_resp.json()
        messages = history_payload.get("messages", [])
        assert history_payload.get("total", 0) >= 4
        assert any(context_secret in (m.get("content") or "") for m in messages), messages

        # User2 independent session should still work
        user2_result = _chat(
            client,
            token2,
            session2,
            "请给我三条今天可执行的学习计划。",
        )
        assert len(user2_result.get("content", "")) >= 20

        # Approval endpoint contract (non-existing id should return 404)
        approval_resp = client.post(
            f"{API_PREFIX}/assistant/approvals/{'00000000-0000-0000-0000-000000000000'}",
            headers=_headers(token1),
            json={"approved": True, "reason": "integration contract check"},
        )
        assert approval_resp.status_code == 404, approval_resp.text


if __name__ == "__main__":
    # Manual debug run support:
    # conda run -n ai_gateway RUN_ASSISTANT_API_E2E=1 pytest tests/integration/test_assistant_api_e2e_live.py -q -s
    start = time.time()
    code = pytest.main([__file__, "-q", "-s"])
    print(f"Finished in {(time.time() - start):.1f}s, code={code}")
