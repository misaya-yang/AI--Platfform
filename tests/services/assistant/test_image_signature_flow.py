"""Multi-turn image editing — thoughtSignature persistence + replay.

Bug context: Gemini 3.x (gemini-3.1-flash-image-preview / nano-banana-2)
requires the ``thoughtSignature`` from each prior model turn to be replayed
on every subsequent edit request. Older code at
``ai_gateway_core/image/helpers.py:append_image_turns`` deliberately dropped
the signature based on Gemini 2.5-era guidance ("only required for function
calls"). Per Google's 2026 docs that's no longer true for image editing —
omitting the signature triggers either a 400 or silent quality degradation.

These tests lock the round-trip: extracted signature → stored on history →
replayed on the corresponding image part of the next ``contents`` array.
"""

from __future__ import annotations

from ai_gateway_core.image import (
    append_image_turns,
    build_gemini_contents_from_history,
)


# ---------------------------------------------------------------------------
# 1. Signature persisted into history when present on result image
# ---------------------------------------------------------------------------


def test_signature_persisted_when_present_in_result():
    history: list[dict] = []
    result_image = {
        "content_base64": "fake_b64_data",
        "mime_type": "image/png",
        "thought_signature": "sig_abc_123",
    }
    append_image_turns(history, "draw a cat", result_image, "here you go")

    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "model"
    assert history[1]["thought_signature"] == "sig_abc_123"


def test_signature_persisted_with_file_uri_path():
    """File API path (preferred) — signature still attached to the model turn."""
    history: list[dict] = []
    result_image = {
        "content_base64": "fake_b64_data",
        "mime_type": "image/png",
        "thought_signature": "sig_xyz_789",
    }
    append_image_turns(
        history, "draw a dog", result_image, None,
        file_uri="files/abc",
    )

    assert history[1]["file_uri"] == "files/abc"
    assert "image_base64" not in history[1]
    assert history[1]["thought_signature"] == "sig_xyz_789"


def test_no_signature_when_absent_in_result():
    """Backward compat — DashScope/Doubao/older Gemini don't return signatures.

    Append must succeed and not insert a sentinel value."""
    history: list[dict] = []
    result_image = {
        "content_base64": "fake",
        "mime_type": "image/png",
    }
    append_image_turns(history, "draw a fish", result_image, None)

    assert len(history) == 2
    assert "thought_signature" not in history[1]


# ---------------------------------------------------------------------------
# 2. Signature replayed in next-turn contents
# ---------------------------------------------------------------------------


def test_signature_replayed_on_image_part_in_next_turn():
    history: list[dict] = [
        {"role": "user", "text": "draw a cat"},
        {
            "role": "model",
            "mime_type": "image/png",
            "image_base64": "fake_b64",
            "thought_signature": "sig_abc_123",
        },
    ]
    contents = build_gemini_contents_from_history(history, "make it orange")

    assert len(contents) == 3  # prior user + prior model + new user
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    img_part = next(p for p in model_turn["parts"] if "inlineData" in p)
    assert img_part["thoughtSignature"] == "sig_abc_123"


def test_signature_replayed_on_file_uri_path():
    history: list[dict] = [
        {"role": "user", "text": "draw a cat"},
        {
            "role": "model",
            "mime_type": "image/png",
            "file_uri": "files/abc",
            "thought_signature": "sig_xyz_789",
        },
    ]
    contents = build_gemini_contents_from_history(history, "make it bigger")

    img_part = next(p for p in contents[1]["parts"] if "fileData" in p)
    assert img_part["thoughtSignature"] == "sig_xyz_789"


# ---------------------------------------------------------------------------
# 3. Backward compat — old history without signatures still replays cleanly
# ---------------------------------------------------------------------------


def test_old_history_without_signatures_replays_without_error():
    """Sessions saved before the 2026 fix lack signatures.

    Replay must not crash and must omit ``thoughtSignature`` rather than
    setting it to None / empty string (Gemini rejects empty values)."""
    history: list[dict] = [
        {"role": "user", "text": "draw a cat"},
        {
            "role": "model",
            "mime_type": "image/png",
            "image_base64": "fake_b64",
        },
    ]
    contents = build_gemini_contents_from_history(history, "edit it")

    img_part = next(p for p in contents[1]["parts"] if "inlineData" in p)
    assert "thoughtSignature" not in img_part


# ---------------------------------------------------------------------------
# 4. Full round-trip — extract → store → replay
# ---------------------------------------------------------------------------


def test_full_roundtrip_two_turns():
    """Simulates a 2-turn edit session: append turn 1 → replay → append turn 2.

    Both signatures must survive in the second-turn contents array."""
    history: list[dict] = []

    # Turn 1
    result_t1 = {
        "content_base64": "img1_b64",
        "mime_type": "image/png",
        "thought_signature": "sig_turn1",
    }
    append_image_turns(history, "draw a sunset", result_t1, None,
                       file_uri="files/turn1")

    # Mid-turn replay
    contents_t2_request = build_gemini_contents_from_history(
        history, "add some birds",
    )
    img_part_t1 = next(
        p for p in contents_t2_request[1]["parts"] if "fileData" in p
    )
    assert img_part_t1["thoughtSignature"] == "sig_turn1"

    # Turn 2 result lands and gets persisted
    result_t2 = {
        "content_base64": "img2_b64",
        "mime_type": "image/png",
        "thought_signature": "sig_turn2",
    }
    append_image_turns(history, "add some birds", result_t2, None,
                       file_uri="files/turn2")

    # Turn 3 replay must carry both signatures
    contents_t3_request = build_gemini_contents_from_history(
        history, "make sky pink",
    )
    # Two model turns now
    model_turns = [c for c in contents_t3_request if c["role"] == "model"]
    assert len(model_turns) == 2

    sigs_in_order = []
    for mt in model_turns:
        img_part = next(p for p in mt["parts"] if "fileData" in p)
        sigs_in_order.append(img_part["thoughtSignature"])
    assert sigs_in_order == ["sig_turn1", "sig_turn2"]
