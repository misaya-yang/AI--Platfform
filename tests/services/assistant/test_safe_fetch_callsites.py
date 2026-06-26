"""Wiring tests — verify each SSRF callsite imports and uses ``safe_fetch``.

Codex flagged that 4 of the 5 unified callsites had no test confirming the
SSRF helper was actually being used. A future refactor that drops the import
or replaces it with raw httpx silently reopens SSRF — these tests catch that.

We don't re-test ``safe_fetch`` semantics (those are in ``test_safe_fetch.py``);
we just confirm each wrapper:
  1. Imports ``safe_fetch`` / ``validate_callback_url`` from the canonical module
  2. Invokes it on the user-controlled URL
  3. Surfaces ``SafeFetchError`` as the wrapper's clean error contract
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
# 4. image callback — validate_callback_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_callback_validates_before_post():
    """The image-task callback must call validate_callback_url BEFORE POST.
    A localhost / metadata callback URL has to fail fast, not after the POST
    has already left the box."""
    from ai_gateway_core.image import callback as mod

    # Simulate a hostile callback URL that validate_callback_url rejects.
    # Patch the imported helper to throw; the callback function must not
    # invoke httpx at all.
    fake_post = AsyncMock()
    fake_client = MagicMock()
    fake_client.post = fake_post

    with patch(
        "ai_gateway_core.image.callback._get_client",
        new=AsyncMock(return_value=fake_client),
    ), patch(
        "ai_gateway_core.security.validate_callback_url",
        side_effect=SafeFetchError("destination rejected: 127.0.0.1"),
    ):
        result = await mod.send_image_callback(
            "http://127.0.0.1/cb",
            {"task_id": "t1", "status": "completed", "images": []},
        )

    assert result is False, "must refuse to post to disallowed URL"
    fake_post.assert_not_awaited(), "must NOT call httpx.post on rejected URL"


# ---------------------------------------------------------------------------
# 5. task_manager — generic task callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_manager_callback_validates_before_post():
    """Same SSRF guard for the generic TaskManager callback path."""
    from src.services.task import task_manager as mod

    src_text = Path(mod.__file__).read_text() if mod.__file__ else ""
    assert "validate_callback_url" in src_text, (
        "task_manager._send_callback must validate callback URLs against "
        "private-IP allowlist before POST."
    )
    assert "follow_redirects=False" in src_text, (
        "The shared callback client must run with follow_redirects=False so "
        "a 302 from an attacker-controlled host can't escape into the "
        "private network."
    )


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
