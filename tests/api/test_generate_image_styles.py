"""
Integration tests for /generate-image style handling.

Exercises the public API endpoint function directly (with mocked providers
and session store) to verify end-to-end:
- schema coercion of legacy style strings
- prompt-modifier injection for Gemini
- DashScope native-tag resolution + negative prompt
- session-level style lock with explicit override
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from assistant_service.api.routes.images import (
    AsyncImageGenerationRequest,
    ImageGenerationRequest,
    generate_image,
)
from assistant_service.auth import UserContext
from assistant_service.core.tools.gemini_image_tool import GeminiImageResult
from assistant_service.core.tools.smart_image_generator import (
    SmartImageGenerationResult,
)
from ai_gateway_core.enums import StylePreset


# =============================================================================
# Helpers
# =============================================================================


def _make_request(session_manager=None) -> SimpleNamespace:
    """Minimal Request stand-in. The endpoint only reads ``.app.state``."""
    app_state = SimpleNamespace(
        session_manager=session_manager,
        multi_rate_limiter=None,  # enforce_rate_limit is a no-op without this
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=app_state),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _user() -> UserContext:
    return UserContext(user_id="u1", tenant_id="t1", is_authenticated=True)


def _registry_stub(provider: str = "google") -> MagicMock:
    """Model registry stub: every lookup returns a model with the given provider."""
    reg = MagicMock()
    model_info = MagicMock()
    model_info.provider = MagicMock()
    model_info.provider.value = provider
    reg.get_model.return_value = model_info
    return reg


def _successful_gemini_image() -> GeminiImageResult:
    return GeminiImageResult(
        success=True,
        images=[{
            "filename": "gemini_1.png",
            "content_base64": "ZmFrZQ==",  # "fake"
            "mime_type": "image/png",
            "size_bytes": 4,
        }],
        text=None,
        duration_ms=123.0,
    )


def _successful_router_result(provider: str = "google") -> SmartImageGenerationResult:
    return SmartImageGenerationResult(
        success=True,
        provider=provider,
        images=[{
            "filename": "img_1.png",
            "content_base64": "ZmFrZQ==",
            "mime_type": "image/png",
            "size_bytes": 4,
        }],
        duration_ms=42.0,
    )


# =============================================================================
# Schema coercion (happens before the endpoint even runs)
# =============================================================================


class TestSchemaStyleCoercion:
    def test_enum_value_accepted(self):
        req = ImageGenerationRequest(prompt="cat", model_id="gemini-2.5-flash-image", style="anime")
        assert req.style is StylePreset.ANIME

    def test_legacy_friendly_name_coerced(self):
        req = ImageGenerationRequest(prompt="cat", model_id="gemini-2.5-flash-image", style="oil")
        assert req.style is StylePreset.OIL_PAINT

    def test_legacy_dashscope_tag_coerced(self):
        req = ImageGenerationRequest(
            prompt="cat", model_id="wanx-v1", style="<photography>",
        )
        assert req.style is StylePreset.REALISTIC

    def test_unknown_string_becomes_default(self):
        req = ImageGenerationRequest(
            prompt="cat", model_id="gemini-2.5-flash-image", style="holographic_xyz",
        )
        assert req.style is StylePreset.DEFAULT

    def test_missing_style_defaults(self):
        req = ImageGenerationRequest(prompt="cat", model_id="gemini-2.5-flash-image")
        assert req.style is StylePreset.DEFAULT

    def test_async_request_coerces_too(self):
        req = AsyncImageGenerationRequest(
            prompt="cat", model_id="gemini-2.5-flash-image", style="3d",
        )
        assert req.style is StylePreset.RENDER_3D


# =============================================================================
# Single-turn: style forwarded to smart router
# =============================================================================


@pytest.mark.asyncio
class TestSingleTurnStyleForwarding:
    async def test_gemini_receives_styled_prompt(self):
        """Gemini route: smart router must get a prompt with the Style: modifier."""
        body = ImageGenerationRequest(
            prompt="a mosque at sunset",
            model_id="gemini-2.5-flash-image",
            style="watercolor",
        )
        router_mock = MagicMock()
        router_mock.generate = AsyncMock(return_value=_successful_router_result("google"))

        with patch(
            "assistant_service.api.routes.images.get_smart_image_generator",
            return_value=router_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=None),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        router_mock.generate.assert_called_once()
        kwargs = router_mock.generate.call_args.kwargs
        assert "a mosque at sunset" in kwargs["prompt"]
        assert "Style:" in kwargs["prompt"]
        assert "watercolor" in kwargs["prompt"].lower()
        # DashScope artefacts are populated regardless — used if Gemini fails.
        assert kwargs["style"] == "<watercolor>"
        assert "digital render" in kwargs["negative_prompt"]

    async def test_dashscope_receives_native_tag_for_tagged_preset(self):
        body = ImageGenerationRequest(
            prompt="a cat", model_id="wanx-v1", style="anime",
        )
        router_mock = MagicMock()
        router_mock.generate = AsyncMock(return_value=_successful_router_result("dashscope"))

        with patch(
            "assistant_service.api.routes.images.get_smart_image_generator",
            return_value=router_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=None),
                user=_user(),
                model_registry=_registry_stub("dashscope"),
            )

        kwargs = router_mock.generate.call_args.kwargs
        assert kwargs["style"] == "<anime>"
        # Modifier is also injected into prompt — harmless for DashScope,
        # critical if DashScope later fails and we fall back to Gemini.
        assert "Style:" in kwargs["prompt"]

    async def test_dashscope_auto_tag_for_untagged_preset(self):
        """Abstract/pixel/comic have no native DashScope tag → <auto>,
        but the modifier still lives in the prompt."""
        body = ImageGenerationRequest(
            prompt="fractals", model_id="wanx-v1", style="pixel_art",
        )
        router_mock = MagicMock()
        router_mock.generate = AsyncMock(return_value=_successful_router_result("dashscope"))

        with patch(
            "assistant_service.api.routes.images.get_smart_image_generator",
            return_value=router_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=None),
                user=_user(),
                model_registry=_registry_stub("dashscope"),
            )

        kwargs = router_mock.generate.call_args.kwargs
        assert kwargs["style"] == "<auto>"
        assert "pixel art" in kwargs["prompt"].lower()

    async def test_default_preset_leaves_prompt_untouched(self):
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-2.5-flash-image", style="default",
        )
        router_mock = MagicMock()
        router_mock.generate = AsyncMock(return_value=_successful_router_result("google"))

        with patch(
            "assistant_service.api.routes.images.get_smart_image_generator",
            return_value=router_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=None),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        kwargs = router_mock.generate.call_args.kwargs
        assert kwargs["prompt"] == "a cat"
        assert kwargs["style"] == "<auto>"
        assert kwargs["negative_prompt"] == ""


# =============================================================================
# Multi-turn: style lock + explicit override
# =============================================================================


def _session_stub(metadata: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.session_id = "sess-1"
    s.metadata = metadata or {}
    return s


def _session_manager_stub(session) -> MagicMock:
    mgr = MagicMock()
    mgr.get = AsyncMock(return_value=session)
    mgr.update_metadata = AsyncMock()
    return mgr


@pytest.mark.asyncio
class TestMultiTurnStyleLock:
    async def test_first_turn_writes_style_to_session(self):
        """First turn: no lock yet, caller's preset becomes the lock."""
        body = ImageGenerationRequest(
            prompt="a mosque",
            model_id="gemini-2.5-flash-image",
            style="oil_paint",
            session_id="sess-1",
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(return_value=_successful_gemini_image())

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=session_mgr),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        # Gemini saw the styled prompt as the last turn's user text.
        contents = gemini_mock.generate_chat.call_args.kwargs["contents"]
        last_turn_text = contents[-1]["parts"][0]["text"]
        assert "a mosque" in last_turn_text
        assert "oil painting" in last_turn_text.lower()

        # Lock persisted for the next turn.
        session_mgr.update_metadata.assert_awaited_once()
        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        assert persisted_meta["style_preset"] == "oil_paint"

    async def test_follow_up_inherits_locked_style_when_default_sent(self):
        """If the client doesn't specify a style on turn 2, session lock applies."""
        body = ImageGenerationRequest(
            prompt="add a minaret",
            model_id="gemini-2.5-flash-image",
            style="default",  # explicitly default → inherit lock
            session_id="sess-1",
        )
        session = _session_stub(metadata={
            "image_chat_history": [
                {"role": "user", "text": "a mosque"},
                {"role": "model", "image_base64": "ZmFrZQ==", "mime_type": "image/png"},
            ],
            "style_preset": "oil_paint",
        })
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(return_value=_successful_gemini_image())

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=session_mgr),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        contents = gemini_mock.generate_chat.call_args.kwargs["contents"]
        last_turn_text = contents[-1]["parts"][0]["text"]
        assert "add a minaret" in last_turn_text
        assert "oil painting" in last_turn_text.lower(), \
            "Turn 2 should inherit oil_paint from session lock"

        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        assert persisted_meta["style_preset"] == "oil_paint"

    async def test_explicit_style_overrides_session_lock(self):
        """Turn 2 sends a non-default style → lock is replaced, prompt uses new style."""
        body = ImageGenerationRequest(
            prompt="redraw it",
            model_id="gemini-2.5-flash-image",
            style="anime",  # overrides oil_paint lock
            session_id="sess-1",
        )
        session = _session_stub(metadata={
            "image_chat_history": [
                {"role": "user", "text": "a mosque"},
                {"role": "model", "image_base64": "ZmFrZQ==", "mime_type": "image/png"},
            ],
            "style_preset": "oil_paint",
        })
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(return_value=_successful_gemini_image())

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=session_mgr),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        contents = gemini_mock.generate_chat.call_args.kwargs["contents"]
        last_turn_text = contents[-1]["parts"][0]["text"]
        assert "anime" in last_turn_text.lower()
        assert "oil painting" not in last_turn_text.lower(), \
            "oil_paint lock should be replaced by anime"

        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        assert persisted_meta["style_preset"] == "anime"

    async def test_failure_does_not_overwrite_session_lock(self):
        """If Gemini fails on turn 2, the session's existing style_preset must
        stay intact — a transient error must not corrupt the user's style
        choice and force them to re-select on retry."""
        body = ImageGenerationRequest(
            prompt="redraw it",
            model_id="gemini-2.5-flash-image",
            style="anime",  # would override if we got that far
            session_id="sess-1",
        )
        session = _session_stub(metadata={
            "image_chat_history": [
                {"role": "user", "text": "a mosque"},
                {"role": "model", "image_base64": "ZmFrZQ==", "mime_type": "image/png"},
            ],
            "style_preset": "oil_paint",
        })
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        # Simulate a safety block — failure path.
        gemini_mock.generate_chat = AsyncMock(return_value=GeminiImageResult(
            success=False,
            error="blocked",
            error_code="GEMINI_IMAGE_BLOCKED",
            blocked=True,
            block_reason="SAFETY",
            duration_ms=1.0,
        ))

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=session_mgr),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        # update_metadata must NOT have been called — the lock stays as oil_paint.
        session_mgr.update_metadata.assert_not_awaited()

    async def test_history_stores_raw_prompt_not_styled(self):
        """Regression guard: we persist the user's raw prompt in history so that
        a later style switch doesn't carry the old modifier into every future
        turn's context (which would compound and confuse the model)."""
        body = ImageGenerationRequest(
            prompt="a mosque",
            model_id="gemini-2.5-flash-image",
            style="oil_paint",
            session_id="sess-1",
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(return_value=_successful_gemini_image())

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body,
                request=_make_request(session_manager=session_mgr),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history = persisted_meta["image_chat_history"]
        # First entry is the user turn — text must be the raw prompt.
        user_turn = next(t for t in history if t.get("role") == "user")
        assert user_turn["text"] == "a mosque"
        assert "oil painting" not in user_turn["text"].lower()


@pytest.mark.asyncio
class TestMultiTurnSignatureFlow:
    """End-to-end: thoughtSignature must round-trip from Gemini response →
    session metadata → next turn's contents array.

    Without this Gemini 3.x silently degrades multi-turn edit quality (or 400s
    outright). See ``ai_gateway_core/image/helpers.py`` for the contract."""

    def _gemini_with_signature(self, sig: str) -> GeminiImageResult:
        return GeminiImageResult(
            success=True,
            images=[{
                "filename": "g.png",
                "content_base64": "ZmFrZQ==",
                "mime_type": "image/png",
                "size_bytes": 4,
                "thought_signature": sig,
            }],
            text=None,
            duration_ms=10.0,
        )

    def _artifact_storage_stub(self):
        """Stub ArtifactStorage. Records artifacts created and serves them
        back from `download_artifact` so multi-turn replay can resolve
        ``artifact_id`` → bytes without any real S3."""
        store: dict[str, bytes] = {}
        next_id = {"n": 0}

        async def _create(*, content, **kw):
            next_id["n"] += 1
            aid = f"art_{next_id['n']:03d}"
            store[aid] = content
            artifact = MagicMock()
            artifact.artifact_id = aid
            return artifact

        async def _presign(artifact, **kw):
            return f"https://s3.example.com/{artifact.artifact_id}"

        async def _download(aid):
            return store.get(aid)

        storage = MagicMock()
        storage.create_artifact = AsyncMock(side_effect=_create)
        storage.get_presigned_download_url = AsyncMock(side_effect=_presign)
        storage.download_artifact = AsyncMock(side_effect=_download)
        storage._store = store  # for test inspection
        return storage

    async def test_signature_and_artifact_id_persist_to_session_on_first_turn(self):
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-3.1-flash-image-preview",
            session_id="sess-1",
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)
        storage = self._artifact_storage_stub()

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_first_turn"),
        )

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        # Response carries presigned URL + artifact_id (no base64).
        assert resp.success is True
        assert len(resp.images) == 1
        assert resp.images[0].url.startswith("https://s3.example.com/")
        assert resp.images[0].artifact_id is not None
        assert "base64" not in resp.images[0].url

        # Session history carries pointer + signature, no bytes.
        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history = persisted_meta["image_chat_history"]
        model_turn = next(t for t in history if t.get("role") == "model")
        assert model_turn.get("thought_signature") == "sig_first_turn"
        assert model_turn.get("artifact_id") is not None
        assert "image_base64" not in model_turn
        assert "file_uri" not in model_turn

    async def test_signature_replayed_to_gemini_on_second_turn(self):
        """Turn 2: history has artifact_id pointer → AS inflates from
        artifact storage → signature MUST be on the inlineData part Gemini
        sees in contents."""
        body = ImageGenerationRequest(
            prompt="make it orange",
            model_id="gemini-3.1-flash-image-preview",
            session_id="sess-1",
        )
        session = _session_stub(metadata={
            "image_chat_history": [
                {"role": "user", "text": "a cat"},
                {
                    "role": "model",
                    "artifact_id": "art_001",
                    "mime_type": "image/png",
                    "thought_signature": "sig_from_turn1",
                },
            ],
        })
        session_mgr = _session_manager_stub(session)
        storage = self._artifact_storage_stub()
        storage._store["art_001"] = b"prior_image_bytes"  # what S3 has

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_turn2"),
        )

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        # Gemini's contents must carry signature on the inflated inlineData part.
        contents = gemini_mock.generate_chat.call_args.kwargs["contents"]
        model_turns = [c for c in contents if c["role"] == "model"]
        assert len(model_turns) == 1
        img_part = next(p for p in model_turns[0]["parts"] if "inlineData" in p)
        assert img_part.get("thoughtSignature") == "sig_from_turn1"
        # Bytes inflated from S3 stub.
        import base64 as _b64
        assert img_part["inlineData"]["data"] == _b64.b64encode(b"prior_image_bytes").decode()

    async def test_watermark_uses_separate_artifact_for_history_vs_response(self):
        """Regression guard: with add_watermark=true, history must store the RAW
        artifact (so next-turn Gemini sees unwatermarked image), while the
        response URL points to the WATERMARKED artifact."""
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-3.1-flash-image-preview",
            session_id="sess-wm", add_watermark=True,
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)
        storage = self._artifact_storage_stub()

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_wm"),
        )

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        # Two artifacts created: one raw, one watermarked
        assert storage.create_artifact.call_count == 2
        sources = [c.kwargs["source"] for c in storage.create_artifact.call_args_list]
        assert "image_generation" in sources
        assert "image_generation_watermarked" in sources

        # Response artifact_id is the watermarked one
        response_artifact_id = resp.images[0].artifact_id
        # History artifact_id is the raw one
        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history = persisted_meta["image_chat_history"]
        model_turn = next(t for t in history if t.get("role") == "model")
        history_artifact_id = model_turn["artifact_id"]
        # They must be different artifacts
        assert response_artifact_id != history_artifact_id, (
            "Response should point to watermarked artifact; history should "
            "point to raw artifact, so next-turn replay doesn't accumulate watermarks."
        )

    async def test_no_watermark_uses_single_artifact(self):
        """When add_watermark=False (default), only one artifact is created and
        response URL + history both reference it."""
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-3.1-flash-image-preview",
            session_id="sess-nowm", add_watermark=False,
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)
        storage = self._artifact_storage_stub()

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_nowm"),
        )

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        assert storage.create_artifact.call_count == 1
        response_artifact_id = resp.images[0].artifact_id
        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history_artifact_id = persisted_meta["image_chat_history"][1]["artifact_id"]
        assert response_artifact_id == history_artifact_id

    async def test_n_greater_than_1_with_session_id_returns_all_images(self):
        """Regression guard — earlier code dropped images[1..n] for stateful + n>1."""
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-3.1-flash-image-preview",
            session_id="sess-multi", n=2,
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)
        storage = self._artifact_storage_stub()

        # Gemini returns 2 images
        multi_image_result = GeminiImageResult(
            success=True,
            images=[
                {"filename": "g1.png", "content_base64": "ZmFrZTE=",
                 "mime_type": "image/png", "size_bytes": 5,
                 "thought_signature": "sig_a"},
                {"filename": "g2.png", "content_base64": "ZmFrZTI=",
                 "mime_type": "image/png", "size_bytes": 5,
                 "thought_signature": "sig_b"},
            ],
            text=None,
            duration_ms=10.0,
        )

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(return_value=multi_image_result)

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        # All n images returned
        assert resp.success is True
        assert len(resp.images) == 2
        assert all(im.artifact_id is not None for im in resp.images)
        assert resp.images[0].artifact_id != resp.images[1].artifact_id

        # History only has ONE model turn (canonical anchor) — not 2 — to keep
        # session metadata small
        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history = persisted_meta["image_chat_history"]
        model_turns = [t for t in history if t.get("role") == "model"]
        assert len(model_turns) == 1


@pytest.mark.asyncio
class TestReferenceImageUrl:
    """Stateless edit via URL — Dev backend passes a URL, AS fetches and
    forwards bytes to Gemini. Saves ~1.3 MB Dev → AS upload per edit.

    SSRF guards (see ``_safe_fetch_url``):
    - http(s) only
    - DNS resolution rejects private/loopback/link-local/reserved/multicast
    - manual redirect handling (max 3 hops, each re-validated)
    - streaming with hard 8 MB byte cap
    """

    async def test_reference_image_url_fetched_and_forwarded_to_gemini(self):
        body = ImageGenerationRequest(
            prompt="make it red",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url="https://example.com/prior.png",
        )

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate = AsyncMock(return_value=GeminiImageResult(
            success=True,
            images=[{
                "filename": "out.png",
                "content_base64": "ZmFrZQ==",
                "mime_type": "image/png",
                "size_bytes": 4,
            }],
            text=None,
            duration_ms=5.0,
        ))

        from unittest.mock import MagicMock as _MM

        async def _one_chunk():
            yield b"prior_image_bytes"

        stream_response = _MM()
        stream_response.status_code = 200
        stream_response.headers = {}
        stream_response.raise_for_status = _MM()
        stream_response.aiter_bytes = _MM(return_value=_one_chunk())
        stream_response.__aenter__ = AsyncMock(return_value=stream_response)
        stream_response.__aexit__ = AsyncMock(return_value=None)

        client_cm = _MM()
        client_cm.__aenter__ = AsyncMock(return_value=client_cm)
        client_cm.__aexit__ = AsyncMock(return_value=None)
        client_cm.stream = _MM(return_value=stream_response)

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._is_safe_destination",
            return_value=(True, ""),
        ), patch(
            "assistant_service.api.routes.images.httpx.AsyncClient",
            return_value=client_cm,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=None,  # data URL fallback for response
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )

        assert resp.success is True
        # Gemini received the bytes we fetched, base64-encoded.
        kwargs = gemini_mock.generate.call_args.kwargs
        import base64 as _b64
        expected_b64 = _b64.b64encode(b"prior_image_bytes").decode()
        assert kwargs["reference_image"] == expected_b64

    async def test_reference_image_url_rejects_non_http_scheme(self):
        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url="file:///etc/passwd",
        )
        from fastapi import HTTPException as _HE

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 400
        assert "http" in str(exc.value.detail).lower()

    @pytest.mark.parametrize("bad_url", [
        "http://localhost/x.png",
        "http://127.0.0.1/x.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/x.png",
        "http://192.168.1.1/x.png",
        "http://172.16.0.1/x.png",
    ])
    async def test_reference_image_url_rejects_private_destinations(self, bad_url):
        """SSRF guard — DNS resolution must reject any private/loopback IP target,
        even before any HTTP fetch is attempted."""
        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url=bad_url,
        )
        from fastapi import HTTPException as _HE

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 400
        assert (
            "rejected" in str(exc.value.detail).lower()
            or "disallowed" in str(exc.value.detail).lower()
        )

    async def test_reference_image_url_rejects_redirect_to_private(self):
        """SSRF guard — a public URL that redirects to a private IP must be
        rejected on the second hop (manual redirect handling re-validates each
        hop)."""
        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url="https://example.com/redirect.png",
        )
        from fastapi import HTTPException as _HE
        from unittest.mock import MagicMock as _MM

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        # First hop: public DNS OK, server returns 302 → http://127.0.0.1/x.png
        # Second hop: DNS validation rejects 127.0.0.1 → 400
        redirect_response = _MM()
        redirect_response.status_code = 302
        redirect_response.headers = {"location": "http://127.0.0.1/x.png"}
        redirect_response.aiter_bytes = AsyncMock()
        redirect_response.__aenter__ = AsyncMock(return_value=redirect_response)
        redirect_response.__aexit__ = AsyncMock(return_value=None)

        client_cm = _MM()
        client_cm.__aenter__ = AsyncMock(return_value=client_cm)
        client_cm.__aexit__ = AsyncMock(return_value=None)
        client_cm.stream = _MM(return_value=redirect_response)

        # Bypass _is_safe_destination only for the public first hop. The second
        # hop (against 127.0.0.1) must run for real and be rejected.
        from assistant_service.api.routes import images as _images

        original_is_safe = _images._is_safe_destination

        def _bypass_first_then_real(host, port):
            if host == "example.com":
                return (True, "")
            return original_is_safe(host, port)

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._is_safe_destination",
            side_effect=_bypass_first_then_real,
        ), patch(
            "assistant_service.api.routes.images.httpx.AsyncClient",
            return_value=client_cm,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 400

    async def test_reference_image_url_streaming_aborts_oversize(self):
        """SSRF guard — body must be aborted as soon as cumulative bytes cross
        8 MB, not after full download."""
        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url="https://example.com/big.png",
        )
        from fastapi import HTTPException as _HE
        from unittest.mock import MagicMock as _MM

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        # Simulate a stream of 1 MB chunks — should abort after the 9th chunk.
        async def _chunks():
            for _ in range(20):
                yield b"X" * (1024 * 1024)

        big_response = _MM()
        big_response.status_code = 200
        big_response.headers = {}
        big_response.raise_for_status = _MM()
        big_response.aiter_bytes = _MM(return_value=_chunks())
        big_response.__aenter__ = AsyncMock(return_value=big_response)
        big_response.__aexit__ = AsyncMock(return_value=None)

        client_cm = _MM()
        client_cm.__aenter__ = AsyncMock(return_value=client_cm)
        client_cm.__aexit__ = AsyncMock(return_value=None)
        client_cm.stream = _MM(return_value=big_response)

        # Patch _is_safe_destination so we don't make real DNS in unit tests.
        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._is_safe_destination",
            return_value=(True, ""),
        ), patch(
            "assistant_service.api.routes.images.httpx.AsyncClient",
            return_value=client_cm,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 400
        assert "8" in str(exc.value.detail)
