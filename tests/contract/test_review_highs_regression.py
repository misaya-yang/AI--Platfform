"""Regression tests for the 3 High-severity review findings
(2026-04-24 code-review round).

H-1: ``ServiceProxy`` must preserve POST/PUT/PATCH/DELETE body across
     the internal retry — a transient ``RemoteProtocolError`` /
     ``PoolTimeout`` on the first attempt must NOT leave the retry
     with an empty body.

H-2: Assistant-service AND knowledge-service must REFUSE TO START
     when ``GATEWAY_ASSISTANT_SHARED_SECRET`` is unset and
     ``allow_anonymous=false``. That combination is a security hole
     (get_user_context trusts X-User-* headers verbatim with no HMAC
     check). Previously the code only logged a misleading warning.

H-3: Knowledge-service ``get_user_context`` must parse ``X-User-Roles``
     the same way assistant-service does. Missing this parse makes
     every KB request see ``roles=["user"]`` regardless of JWT roles.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

import httpx
import pytest
from ai_gateway_core.proxy.base import (
    CircuitBreaker,
    InMemoryCounter,
    ServiceProxy,
    ServiceProxyConfig,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# H-1: body preserved across retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_body_preserved_across_connect_error_retry():
    """First attempt raises ``RemoteProtocolError`` — retry must send the
    same bytes to the upstream, NOT an empty stream.
    """
    cfg = ServiceProxyConfig(name="test", base_url="http://fake")
    proxy = ServiceProxy(
        cfg,
        breaker=CircuitBreaker(name="test", store=InMemoryCounter()),
    )

    attempt_bodies: list[bytes] = []
    attempt_count = {"n": 0}

    async def fake_app(scope, receive, send):
        assert scope["type"] == "http"
        chunks = []
        while True:
            msg = await receive()
            if msg.get("body"):
                chunks.append(msg["body"])
            if not msg.get("more_body"):
                break
        attempt_bodies.append(b"".join(chunks))
        attempt_count["n"] += 1
        if attempt_count["n"] == 1:
            raise httpx.RemoteProtocolError("simulated upstream flap")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b'{"ok": true}'})

    transport = httpx.ASGITransport(app=fake_app)
    client = httpx.AsyncClient(transport=transport, base_url="http://fake")

    async def fake_get_client():
        return client

    async def noop_reset():
        pass

    proxy._get_client = fake_get_client  # type: ignore[assignment]
    proxy._reset_client = noop_reset  # type: ignore[assignment]

    gw = FastAPI()

    @gw.post("/gw/test")
    async def _route(request: Request):
        return await proxy.forward(
            request,
            user_headers={"X-User-Id": "u", "X-Tenant-Id": "t"},
            upstream_path="/upstream",
        )

    with TestClient(gw) as c:
        r = c.post(
            "/gw/test",
            json={"hello": "world"},
            headers={"Idempotency-Key": "body-replay-regression"},
        )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert attempt_count["n"] == 2
    assert attempt_bodies[0] != b"", "first attempt body was empty"
    assert attempt_bodies[0] == attempt_bodies[1], (
        f"body changed between attempts: {attempt_bodies!r}"
    )
    assert b'"hello"' in attempt_bodies[1]


# ---------------------------------------------------------------------------
# H-2: refuse to start when secret unset + allow_anonymous=false
# ---------------------------------------------------------------------------


def test_assistant_service_main_fails_hard_on_missing_secret_no_anonymous():
    """Source-inspection guard: the old warning-only pattern must be
    gone and the new RuntimeError must be in place.
    """
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "assistant-service"
        / "src"
        / "assistant_service"
        / "main.py"
    )
    source = src_path.read_text()
    assert "every request will be rejected by get_user_context" not in source
    assert "raise RuntimeError(" in source
    assert "GATEWAY_ASSISTANT_SHARED_SECRET" in source
    assert "security hole" in source
    assert "validate_gateway_auth_configuration(" in source
    assert 'allow_anonymous_setting="ASSISTANT_APP__ALLOW_ANONYMOUS"' in source


def test_knowledge_service_main_fails_hard_on_missing_secret_no_anonymous():
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "knowledge-service"
        / "src"
        / "knowledge_service"
        / "main.py"
    )
    source = src_path.read_text()
    assert "gateway_secret_unset_but_anonymous_disabled" not in source
    assert "raise RuntimeError(" in source
    assert "security hole" in source
    assert "validate_gateway_auth_configuration(" in source
    assert 'allow_anonymous_setting="KNOWLEDGE_APP__ALLOW_ANONYMOUS"' in source


# ---------------------------------------------------------------------------
# H-3: knowledge-service parses X-User-Roles
# ---------------------------------------------------------------------------


def test_knowledge_service_user_context_parses_roles():
    """knowledge-service is not in the root uv workspace, so we can't
    ``import knowledge_service`` here. Instead, inspect the source to
    confirm the same 3 semantics AS-side already has:

      1. ``X-User-Roles`` header is read via ``.strip()``
      2. comma-split with empty-entry filtering
      3. default to ``["user"]`` when the header is missing / empty
      4. the UserContext is constructed with ``roles=roles``

    Mirror test to the AS-side ``test_auth_e2e.py`` — if AS behaviour
    ever changes, the two tests drift and the drift is visible.
    """
    from pathlib import Path

    src_path = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "knowledge-service"
        / "src"
        / "knowledge_service"
        / "auth"
        / "user_context.py"
    )
    source = src_path.read_text()

    # (1) reads the header
    assert 'X-User-Roles' in source, "KB user_context doesn't read X-User-Roles"
    # (2) comma-split + strip + empty filter
    assert 'split(",")' in source
    assert "r.strip()" in source or "r.strip() for r" in source
    # (3) default to ["user"]
    assert '["user"]' in source
    # (4) constructs UserContext with roles=roles
    assert "roles=roles" in source


def test_knowledge_service_roles_parse_shape_matches_assistant_service():
    """Parallel invariant: both sides contain the same key idioms so
    parse behaviour stays aligned. Too coarse to catch every drift,
    but catches "KB dropped the parse again" and "AS changed default".
    """
    from pathlib import Path

    kb = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "knowledge-service"
        / "src"
        / "knowledge_service"
        / "auth"
        / "user_context.py"
    ).read_text()
    as_ = (
        Path(__file__).resolve().parents[2]
        / "apps"
        / "assistant-service"
        / "src"
        / "assistant_service"
        / "auth"
        / "user_context.py"
    ).read_text()

    # Every shape check that holds on AS must also hold on KB.
    for fragment in (
        'X-User-Roles',
        'split(",")',
        'r.strip() for r',
        '["user"]',
        'roles=roles',
    ):
        assert fragment in as_, f"AS impl lost idiom {fragment!r}"
        assert fragment in kb, (
            f"KB impl missing idiom {fragment!r} — cross-service drift from AS"
        )


# ---------------------------------------------------------------------------
# Model-config watcher: a dropped Redis connection must not silently kill the
# invalidation subscription (bounded TTL is only the degraded fallback).
# ---------------------------------------------------------------------------


class _RecordingResolver:
    def __init__(self) -> None:
        self.invalidations: list[dict[str, Any]] = []

    async def invalidate(self, **kwargs: Any) -> None:
        self.invalidations.append(kwargs)


class _ScriptedPubSub:
    def __init__(self, deliver: int, then: str) -> None:
        self.deliver = deliver
        self.then = then  # "fail" raises, "hang" blocks until cancelled
        self.subscribed_channels: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_channels.append(channel)

    async def listen(self):
        for _index in range(self.deliver):
            yield {
                "type": "message",
                "data": json.dumps(
                    {"tenant_id": f"tenant-{len(self.subscribed_channels)}"}
                ),
            }
        if self.then == "fail":
            raise ConnectionError("redis connection lost")
        await asyncio.Event().wait()

    async def unsubscribe(self, channel: str) -> None:
        del channel
        return None

    async def aclose(self) -> None:
        self.closed = True


class _ScriptedRedis:
    def __init__(self) -> None:
        self.pubsubs: list[_ScriptedPubSub] = []

    def pubsub(self) -> _ScriptedPubSub:
        pubsub = _ScriptedPubSub(
            deliver=1,
            then="fail" if not self.pubsubs else "hang",
        )
        self.pubsubs.append(pubsub)
        return pubsub


@pytest.mark.asyncio
async def test_model_config_watch_reconnects_after_redis_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "test-only-shared-secret")
    from assistant_service import main as assistant_main

    redis_client = _ScriptedRedis()
    resolver = _RecordingResolver()

    task = asyncio.create_task(
        assistant_main._watch_model_config_changes(redis_client, resolver)
    )
    try:
        deadline = time.monotonic() + 15.0
        while len(resolver.invalidations) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert len(resolver.invalidations) == 2
        assert len(redis_client.pubsubs) == 2
        assert redis_client.pubsubs[0].subscribed_channels == [
            "gateway:model-config:changed:v1"
        ]
        assert redis_client.pubsubs[0].closed
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
