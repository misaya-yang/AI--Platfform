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

    async def test_provider_over_return_is_capped_to_requested_n(self):
        body = ImageGenerationRequest(
            prompt="a red square",
            model_id="gemini-2.5-flash-image",
            style="default",
            n=1,
        )
        router_mock = MagicMock()
        router_mock.generate = AsyncMock(return_value=SmartImageGenerationResult(
            success=True,
            provider="google",
            images=[
                {
                    "filename": "img_1.png",
                    "content_base64": "ZmFrZTE=",
                    "mime_type": "image/png",
                    "size_bytes": 5,
                },
                {
                    "filename": "img_2.png",
                    "content_base64": "ZmFrZTI=",
                    "mime_type": "image/png",
                    "size_bytes": 5,
                },
            ],
            duration_ms=42.0,
        ))

        with patch(
            "assistant_service.api.routes.images.get_smart_image_generator",
            return_value=router_mock,
        ):
            resp = await generate_image(
                body=body,
                request=_make_request(session_manager=None),
                user=_user(),
                model_registry=_registry_stub("google"),
            )

        assert resp.success is True
        assert len(resp.images) == 1
        assert router_mock.generate.call_args.kwargs["n"] == 1

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
    """Stateless edit via URL — route layer wires ``safe_fetch`` correctly.

    Deep SSRF behavior (private IPs, redirect re-validation, streaming cap,
    DNS rebinding defense) is exhaustively covered in
    ``tests/services/assistant/test_safe_fetch.py``. Here we only verify
    the route-level integration: ``safe_fetch`` is called with the user's
    URL, its bytes flow into Gemini, and SafeFetchError surfaces as a 400.
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
            images=[{"filename": "out.png", "content_base64": "ZmFrZQ==",
                     "mime_type": "image/png", "size_bytes": 4}],
            text=None, duration_ms=5.0,
        ))

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images.safe_fetch",
            new=AsyncMock(return_value=b"prior_image_bytes"),
        ) as mock_fetch, patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=None,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )

        assert resp.success is True
        # Route called safe_fetch with the user's URL
        mock_fetch.assert_awaited_once()
        assert mock_fetch.await_args.args[0] == "https://example.com/prior.png"

        # Bytes safe_fetch returned were forwarded to Gemini base64-encoded
        import base64 as _b64
        kwargs = gemini_mock.generate.call_args.kwargs
        assert kwargs["reference_image"] == _b64.b64encode(b"prior_image_bytes").decode()

    async def test_reference_image_url_safefetch_error_becomes_400(self):
        """When safe_fetch raises (private IP, oversize, etc.), the route must
        return a 400 — not crash with 500 or fall through to fresh generation."""
        from ai_gateway_core.security import SafeFetchError
        from fastapi import HTTPException as _HE

        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_image_url="http://127.0.0.1/x.png",
        )
        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images.safe_fetch",
            new=AsyncMock(side_effect=SafeFetchError("destination rejected: private")),
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 400
        assert "rejected" in str(exc.value.detail).lower()


@pytest.mark.asyncio
class TestReferenceArtifactId:
    """``reference_artifact_id`` — preferred ID-based edit path.

    Server looks up bytes directly via ArtifactStorage. No URL fetch happens,
    so SSRF surface is zero on this path."""

    def _artifact_storage_with(self, artifact_id: str, content: bytes):
        storage = MagicMock()
        # Owner matches the standard _user() (u1/t1) so ownership check passes
        artifact_obj = MagicMock()
        artifact_obj.artifact_id = artifact_id
        artifact_obj.user_id = "u1"
        artifact_obj.tenant_id = "t1"

        async def _get(aid):
            return artifact_obj if aid == artifact_id else None

        async def _download(aid):
            if aid == artifact_id:
                return content
            return None

        storage.get_artifact = AsyncMock(side_effect=_get)
        storage.download_artifact = AsyncMock(side_effect=_download)
        return storage

    async def test_artifact_id_lookup_forwarded_to_gemini(self):
        body = ImageGenerationRequest(
            prompt="add a hat",
            model_id="gemini-3.1-flash-image-preview",
            reference_artifact_id="art_abc",
        )
        storage = self._artifact_storage_with("art_abc", b"original_image_bytes")
        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate = AsyncMock(return_value=GeminiImageResult(
            success=True,
            images=[{"filename": "out.png", "content_base64": "ZmFrZQ==",
                     "mime_type": "image/png", "size_bytes": 4}],
            text=None, duration_ms=5.0,
        ))

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ):
            resp = await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )

        assert resp.success is True
        # No URL fetch — artifact storage was used
        storage.download_artifact.assert_awaited_once_with("art_abc")
        # Gemini saw the artifact bytes
        import base64 as _b64
        kwargs = gemini_mock.generate.call_args.kwargs
        assert kwargs["reference_image"] == _b64.b64encode(b"original_image_bytes").decode()

    async def test_artifact_id_not_found_returns_404(self):
        from fastapi import HTTPException as _HE

        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_artifact_id="art_missing",
        )
        storage = self._artifact_storage_with("art_existing", b"x")
        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestReferenceWithNonGeminiModel:
    """Reference image given but model_id routes to a non-Gemini provider —
    must fail explicitly, not silently produce an unrelated fresh image."""

    @pytest.mark.parametrize("ref_field,ref_value", [
        ("reference_image", "ZmFrZQ=="),
        ("reference_image_url", "https://example.com/x.png"),
        ("reference_artifact_id", "art_abc"),
    ])
    async def test_sync_returns_explicit_error(self, ref_field, ref_value):
        body = ImageGenerationRequest(
            prompt="edit",
            model_id="qwen-image-2.0",  # routes to DashScope, not Gemini
            **{ref_field: ref_value},
        )
        resp = await generate_image(
            body=body, request=_make_request(session_manager=None),
            user=_user(), model_registry=_registry_stub("dashscope"),
        )
        assert resp.success is False
        assert "gemini" in (resp.error or "").lower()


@pytest.mark.asyncio
class TestReferenceArtifactIdIDOR:
    """Cross-tenant IDOR — user A must not be able to use user B's artifact_id."""

    def _artifact_storage_with_owner(self, artifact_id: str, owner_user: str, owner_tenant: str):
        from unittest.mock import MagicMock as _MM
        artifact = _MM()
        artifact.artifact_id = artifact_id
        artifact.user_id = owner_user
        artifact.tenant_id = owner_tenant

        async def _get(aid):
            return artifact if aid == artifact_id else None

        async def _download(aid):
            return b"secret_bytes" if aid == artifact_id else None

        storage = _MM()
        storage.get_artifact = AsyncMock(side_effect=_get)
        storage.download_artifact = AsyncMock(side_effect=_download)
        return storage

    async def test_other_tenant_artifact_id_returns_404(self):
        from fastapi import HTTPException as _HE

        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_artifact_id="art_belongs_to_other",
        )
        # Artifact owner is tenant=other_tenant, user=other_user
        storage = self._artifact_storage_with_owner(
            "art_belongs_to_other", "other_user", "other_tenant",
        )
        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        # Caller is the standard u1/t1 user — should get 404, not the bytes
        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )

        # 404 (not 403) — same code as not-found so attackers can't enumerate
        assert exc.value.status_code == 404
        # Bytes never downloaded for unauthorized caller
        storage.download_artifact.assert_not_awaited()

    async def test_other_tenant_same_user_id_still_blocked(self):
        """Cross-tenant same user_id is suspicious (re-used user id across
        tenants) — must still 404."""
        from fastapi import HTTPException as _HE

        body = ImageGenerationRequest(
            prompt="edit",
            model_id="gemini-3.1-flash-image-preview",
            reference_artifact_id="art_other_tenant",
        )
        # Same user_id="u1" but different tenant_id
        storage = self._artifact_storage_with_owner(
            "art_other_tenant", "u1", "different_tenant",
        )
        gemini_mock = MagicMock()
        gemini_mock.is_configured = True

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ), patch(
            "assistant_service.api.routes.images._get_artifact_storage",
            return_value=storage,
        ), pytest.raises(_HE) as exc:
            await generate_image(
                body=body, request=_make_request(session_manager=None),
                user=_user(), model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
class TestImageTaskOwnership:
    """Ownership check on /image-task/{task_id} — codex flagged that any
    authenticated user could poll any UUID and read another tenant's prompt
    + presigned URLs."""

    async def test_other_user_polling_task_gets_404(self):
        from fastapi import HTTPException as _HE
        from assistant_service.api.routes.images import (
            _image_tasks,
            get_image_task_status,
        )

        # Submit a task as user A (admin/default tenant)
        _image_tasks["task_a1"] = {
            "task_id": "task_a1",
            "status": "completed",
            "progress": 100,
            "prompt": "secret prompt",
            "model_id": "gemini-3-flash-preview",
            "provider": "google",
            "images": [{"url": "https://s3...", "width": 512, "height": 512}],
            "duration_ms": 1000.0,
            "error": None,
            "created_at": "2026-04-27T18:00:00+00:00",
            "completed_at": "2026-04-27T18:00:01+00:00",
            "owner_user_id": "user_a",
            "owner_tenant_id": "tenant_a",
        }

        try:
            # User B polls the task — must 404
            from assistant_service.auth import UserContext
            user_b = UserContext(
                user_id="user_b", tenant_id="tenant_a", is_authenticated=True,
            )
            with pytest.raises(_HE) as exc:
                await get_image_task_status(
                    task_id="task_a1",
                    request=_make_request(session_manager=None),
                    user=user_b,
                )
            assert exc.value.status_code == 404
        finally:
            _image_tasks.pop("task_a1", None)

    async def test_owner_polling_task_succeeds(self):
        from assistant_service.api.routes.images import (
            _image_tasks,
            get_image_task_status,
        )

        _image_tasks["task_owner"] = {
            "task_id": "task_owner",
            "status": "completed",
            "progress": 100,
            "prompt": "p",
            "model_id": "gemini-3-flash-preview",
            "provider": "google",
            "images": [],
            "duration_ms": 1.0,
            "error": None,
            "created_at": "2026-04-27T18:00:00+00:00",
            "completed_at": None,
            "owner_user_id": "u1",
            "owner_tenant_id": "t1",
        }

        try:
            resp = await get_image_task_status(
                task_id="task_owner",
                request=_make_request(session_manager=None),
                user=_user(),
            )
            assert resp.task_id == "task_owner"
            assert resp.status == "completed"
        finally:
            _image_tasks.pop("task_owner", None)
