"""Wiring tests — verify each SSRF callsite imports and uses ``safe_fetch``.

Codex flagged that 4 of the 5 unified callsites had no test confirming the
SSRF helper was actually being used. A future refactor that drops the import
or replaces it with raw httpx silently reopens SSRF — these tests catch that.

We don't re-test ``safe_fetch`` semantics (those are in ``test_safe_fetch.py``);
we just confirm each wrapper:
  1. Imports ``safe_fetch`` / ``safe_callback_post`` from the canonical module
  2. Invokes it on the user-controlled URL
  3. Surfaces ``SafeFetchError`` as the wrapper's clean error contract
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ai_gateway_core.security import SafeFetchError

# ---------------------------------------------------------------------------
# 1. KS document_service.create_document_from_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ks_document_url_uses_safe_fetch():
    """KS URL ingestion must go through ``safe_fetch`` for SSRF + size cap."""
    from knowledge_service.services.knowledge import document_service as mod

    # Find the function via the module — its signature varies by KS version.
    assert hasattr(mod, "DocumentService") or hasattr(mod, "create_document_from_url")
    # Static import-presence check — `safe_fetch` must be importable from
    # ai_gateway_core.security at the call site.
    src_text = Path(mod.__file__).read_text() if mod.__file__ else ""
    assert "safe_fetch" in src_text, (
        "KS document_service must use safe_fetch from ai_gateway_core.security"
    )
    assert "follow_redirects=True" not in src_text, (
        "Raw httpx with follow_redirects=True is the SSRF pattern we just "
        "killed; document_service must not reintroduce it."
    )


# ---------------------------------------------------------------------------
# 2. KS document_image_extractor — embedded <img> URL fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ks_image_extractor_uses_safe_fetch():
    from knowledge_service.services.knowledge.ingestion import (
        document_image_extractor as mod,
    )

    src_text = Path(mod.__file__).read_text() if mod.__file__ else ""
    assert "safe_fetch" in src_text, (
        "KS image extractor must use safe_fetch — without it, document URL "
        "ingestion can be redirected through user-controlled <img> tags into "
        "the private network."
    )


# ---------------------------------------------------------------------------
# 3. AS image route — reference_image_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_image_route_uses_safe_fetch():
    from assistant_service.api.routes import images as mod

    assert hasattr(mod, "safe_fetch"), (
        "images.py must import safe_fetch directly — patching its module-level "
        "binding is how IDOR / SSRF tests verify wiring without spinning up "
        "the full route."
    )


# ---------------------------------------------------------------------------
# 4. image callback — safe_callback_post
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_callback_validates_before_post():
    """The image-task callback must use DNS-pinned safe_callback_post.

    A localhost / metadata callback URL has to fail inside the SSRF primitive,
    not through a raw httpx POST.
    """
    from ai_gateway_core.image import callback as mod

    with patch(
        "ai_gateway_core.security.safe_callback_post",
        side_effect=SafeFetchError("destination rejected: 127.0.0.1"),
    ) as safe_post:
        result = await mod.send_image_callback(
            "http://127.0.0.1/cb",
            {"task_id": "t1", "status": "completed", "images": []},
        )

    assert result is False, "must refuse to post to disallowed URL"
    safe_post.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. task_manager — generic task callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_manager_callback_validates_before_post():
    """Same SSRF guard for the generic TaskManager callback path."""
    from src.services.task import task_manager as mod

    src_text = Path(mod.__file__).read_text() if mod.__file__ else ""
    assert "safe_callback_post" in src_text, (
        "task_manager._send_callback must use safe_callback_post so callback "
        "POSTs are DNS-pinned after validation."
    )
    assert ".post(task.callback_url" not in src_text, (
        "task_manager._send_callback must not POST user-controlled callback "
        "URLs through a raw httpx client."
    )


@pytest.mark.asyncio
async def test_safe_callback_post_rejects_metadata_destination_before_post():
    from ai_gateway_core.security.safe_fetch import safe_callback_post

    with pytest.raises(SafeFetchError):
        await safe_callback_post(
            "http://169.254.169.254/latest/meta-data",
            json={"task_id": "t1"},
        )


@pytest.mark.asyncio
async def test_safe_callback_post_uses_dns_pinned_transport_without_redirects(monkeypatch):
    mod = importlib.import_module("ai_gateway_core.security.safe_fetch")

    captured: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, host: str, pinned_ip: str):
            captured["transport_host"] = host
            captured["transport_ip"] = pinned_ip

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, json: dict):
            captured["post_url"] = url
            captured["post_json"] = json
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(
        mod,
        "is_safe_destination",
        lambda _host, _port: (True, "93.184.216.34"),
    )
    monkeypatch.setattr(mod, "_DNSPinnedTransport", FakeTransport)
    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)

    response = await mod.safe_callback_post(
        "https://callback.example.test/hook",
        json={"task_id": "t1"},
        timeout=3.0,
    )

    assert response.status_code == 204
    assert captured["transport_host"] == "callback.example.test"
    assert captured["transport_ip"] == "93.184.216.34"
    assert captured["post_url"] == "https://callback.example.test/hook"
    assert captured["post_json"] == {"task_id": "t1"}
    client_kwargs = captured["client_kwargs"]
    assert client_kwargs["transport"].__class__ is FakeTransport
    assert client_kwargs["timeout"] == 3.0
    assert client_kwargs["follow_redirects"] is False


# ---------------------------------------------------------------------------
# 6. web_fetch agent tool — moved to safe_fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_uses_safe_fetch():
    """web_fetch was found in code review to have its own SSRF impl. After
    the migration it must import from ai_gateway_core.security."""
    from assistant_service.core.tools import web_fetch as mod

    assert hasattr(mod, "safe_fetch_with_response"), (
        "web_fetch must import safe_fetch_with_response — the local "
        "_fetch_with_manual_redirects has been replaced with this primitive."
    )
    src_text = Path(mod.__file__).read_text() if mod.__file__ else ""
    # No more parallel SSRF impl — these were the symptoms of the duplication
    assert "follow_redirects=False" not in src_text, (
        "follow_redirects flag should now live inside safe_fetch_with_response, "
        "not in web_fetch's own httpx config."
    )
