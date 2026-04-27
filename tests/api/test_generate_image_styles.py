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

    async def test_signature_persists_to_session_on_first_turn(self):
        body = ImageGenerationRequest(
            prompt="a cat", model_id="gemini-3.1-flash-image-preview",
            session_id="sess-1",
        )
        session = _session_stub(metadata={"image_chat_history": []})
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_first_turn"),
        )
        gemini_mock.upload_image = AsyncMock(return_value="files/abc")

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        persisted_meta = session_mgr.update_metadata.call_args.args[1]
        history = persisted_meta["image_chat_history"]
        model_turn = next(t for t in history if t.get("role") == "model")
        assert model_turn.get("thought_signature") == "sig_first_turn", (
            "Signature from Gemini response must land on the model turn — "
            "without it, turn 2 has no anchor for the model's prior reasoning."
        )

    async def test_signature_replayed_to_gemini_on_second_turn(self):
        """Turn 2: history already has a signature → it MUST be in the
        contents array sent to Gemini, attached to the prior image part."""
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
                    "file_uri": "files/abc",
                    "mime_type": "image/png",
                    "thought_signature": "sig_from_turn1",
                },
            ],
        })
        session_mgr = _session_manager_stub(session)

        gemini_mock = MagicMock()
        gemini_mock.is_configured = True
        gemini_mock.generate_chat = AsyncMock(
            return_value=self._gemini_with_signature("sig_turn2"),
        )
        gemini_mock.upload_image = AsyncMock(return_value="files/def")

        with patch(
            "assistant_service.api.routes.images.get_gemini_image_generator",
            return_value=gemini_mock,
        ):
            await generate_image(
                body=body, request=_make_request(session_manager=session_mgr),
                user=_user(), model_registry=_registry_stub("google"),
            )

        contents = gemini_mock.generate_chat.call_args.kwargs["contents"]
        # Find the prior model image part — must carry the signature
        model_turns = [c for c in contents if c["role"] == "model"]
        assert len(model_turns) == 1
        img_part = next(p for p in model_turns[0]["parts"] if "fileData" in p)
        assert img_part.get("thoughtSignature") == "sig_from_turn1", (
            "Replay missing signature → Gemini 3.x will reject or degrade. "
            f"Got img_part={img_part}"
        )
