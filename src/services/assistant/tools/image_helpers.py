"""Shared helpers for image generation endpoints.

Centralizes logic that was previously duplicated between the sync endpoint
(``generate_image``), async task (``_run_image_generation_task``), and the
tool executor (``ImageGeneratorExecutor``).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Size / aspect ratio parsing
# ---------------------------------------------------------------------------

_ASPECT_CANDIDATES = {
    "1:1": 1.0,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
}


def parse_image_size(size: str | None, default: tuple[int, int] = (1024, 1024)) -> tuple[int, int, str]:
    """Parse a ``"WIDTH*HEIGHT"`` string into (width, height, aspect_ratio).

    Returns the default dimensions on any parse failure. Aspect ratio is
    snapped to the closest supported value among 1:1, 16:9, 9:16, 4:3, 3:4.
    """
    width, height = default
    if size:
        try:
            parts = size.split("*")
            if len(parts) == 2:
                width, height = int(parts[0]), int(parts[1])
        except ValueError:
            pass

    try:
        ratio = float(width) / float(height) if height else 1.0
    except Exception:
        ratio = 1.0

    aspect = min(_ASPECT_CANDIDATES.keys(), key=lambda k: abs(ratio - _ASPECT_CANDIDATES[k]))
    return width, height, aspect


# ---------------------------------------------------------------------------
# Style mapping — shared across sync/async endpoints
# ---------------------------------------------------------------------------

STYLE_MAP: dict[str, str] = {
    "default": "<auto>",
    "auto": "<auto>",
    "photography": "<photography>",
    "portrait": "<portrait>",
    "3d": "<3d cartoon>",
    "anime": "<anime>",
    "oil": "<oil painting>",
    "watercolor": "<watercolor>",
    "sketch": "<sketch>",
    "flat": "<flat illustration>",
}


def resolve_style(style: str | None) -> str:
    """Map a friendly style name to the DashScope style tag."""
    return STYLE_MAP.get(style or "default", "<auto>")


# ---------------------------------------------------------------------------
# Provider routing — detect which image backend to prefer from model_id
# ---------------------------------------------------------------------------


def resolve_image_routing(
    model_id: str | None,
    selected_provider: str | None = None,
) -> tuple[bool, bool, str | None]:
    """Determine (prefer_gemini, prefer_doubao, dashscope_model_override).

    Rules:
    - ``selected_provider == "google"`` or ``"gemini"`` in id → Gemini
    - ``"doubao"`` or ``"seedream"`` in id → Doubao
    - ``"qwen-image"`` or ``"qwen_image"`` in id → DashScope with model override

    Detection is case-insensitive and runs a single ``.lower()`` pass.
    """
    if not model_id:
        return False, False, None

    mid = model_id.lower()
    prefer_gemini = selected_provider == "google" or "gemini" in mid
    prefer_doubao = "doubao" in mid or "seedream" in mid
    dashscope_model: str | None = None
    if "qwen-image" in mid or "qwen_image" in mid:
        dashscope_model = model_id  # pass original id as-is, e.g. "qwen-image-2.0"

    return prefer_gemini, prefer_doubao, dashscope_model


# ---------------------------------------------------------------------------
# Gemini multi-turn contents builder
# ---------------------------------------------------------------------------


def build_gemini_contents_from_history(
    image_history: list[dict[str, Any]],
    new_user_text: str,
) -> list[dict[str, Any]]:
    """Build Gemini ``contents`` array from stored image chat history.

    - Each turn becomes ``{role, parts: [...]}``
    - Model-generated images include ``thought_signature`` if present
      (required by Gemini 3.x for multi-turn editing)
    - Appends the new user text as the final turn
    """
    contents: list[dict[str, Any]] = []
    for turn in image_history:
        role = turn.get("role", "user")
        parts: list[dict[str, Any]] = []
        if turn.get("text"):
            parts.append({"text": turn["text"]})
        if turn.get("image_base64"):
            mime = turn.get("mime_type", "image/jpeg")
            img_part: dict[str, Any] = {
                "inlineData": {"mimeType": mime, "data": turn["image_base64"]}
            }
            if turn.get("thought_signature"):
                img_part["thoughtSignature"] = turn["thought_signature"]
            parts.append(img_part)
        if parts:
            contents.append({"role": role, "parts": parts})

    contents.append({"role": "user", "parts": [{"text": new_user_text}]})
    return contents


def append_image_turns(
    image_history: list[dict[str, Any]],
    user_text: str,
    result_image: dict[str, Any] | None,
    result_text: str | None,
) -> None:
    """Append one user turn and (on success) one model turn to ``image_history``.

    Mutates the list in place. Call only when Gemini responded successfully.
    Skip the user turn entirely on failure to avoid a dangling unanswered
    prompt in the next turn's context.
    """
    if result_image is None:
        return
    image_history.append({"role": "user", "text": user_text})
    model_turn: dict[str, Any] = {
        "role": "model",
        "image_base64": result_image["content_base64"],
        "mime_type": result_image.get("mime_type", "image/jpeg"),
    }
    if result_text:
        model_turn["text"] = result_text
    if result_image.get("thought_signature"):
        model_turn["thought_signature"] = result_image["thought_signature"]
    image_history.append(model_turn)
