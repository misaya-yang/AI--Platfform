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

    Each history turn can carry the image either as an inline base64 payload or
    as a Gemini Files API URI. The URI form is preferred because it lets the
    session row stay tiny (~50 bytes per turn vs 1MB+) while Gemini still sees
    the real image on each call.

    - ``file_uri`` turns → ``{"fileData": {"fileUri": ..., "mimeType": ...}}``
    - ``image_base64`` turns → ``{"inlineData": {"data": ..., "mimeType": ...}}``
      (kept for backward-compatibility with history written before the Files
      API migration)
    - ``thought_signature`` is passed through verbatim when present, but is not
      required for image-only turns (Gemini only enforces it for function calls)
    """
    contents: list[dict[str, Any]] = []
    for turn in image_history:
        role = turn.get("role", "user")
        parts: list[dict[str, Any]] = []
        if turn.get("text"):
            parts.append({"text": turn["text"]})

        file_uri = turn.get("file_uri")
        inline_b64 = turn.get("image_base64")
        mime = turn.get("mime_type", "image/jpeg")
        img_part: dict[str, Any] | None = None
        if file_uri:
            img_part = {"fileData": {"fileUri": file_uri, "mimeType": mime}}
        elif inline_b64:
            img_part = {"inlineData": {"mimeType": mime, "data": inline_b64}}

        if img_part is not None:
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
    *,
    file_uri: str | None = None,
) -> None:
    """Append one user turn and (on success) one model turn to ``image_history``.

    When ``file_uri`` is provided (preferred path), the model turn stores only
    the URI — the actual base64 lives on Gemini's Files API side. Without a
    ``file_uri`` we fall back to persisting the base64 inline, which keeps
    behaviour consistent with old sessions but will be rejected by the 1 MB
    session metadata cap and lose the visual anchor on the next turn.

    Mutates the list in place. Call only when Gemini responded successfully;
    skip entirely on failure to avoid a dangling unanswered prompt in the next
    turn's context.
    """
    if result_image is None:
        return
    image_history.append({"role": "user", "text": user_text})
    model_turn: dict[str, Any] = {
        "role": "model",
        "mime_type": result_image.get("mime_type", "image/jpeg"),
    }
    if file_uri:
        model_turn["file_uri"] = file_uri
    else:
        model_turn["image_base64"] = result_image["content_base64"]
    if result_text:
        model_turn["text"] = result_text
    # thought_signature is intentionally NOT persisted: it's a 1MB+ blob that
    # Gemini only strictly requires for function-call parts. For plain image
    # editing turns the docs say "the API does not strictly enforce validation"
    # and omitting it does not trigger a 400. Storing it would push session
    # metadata back over the 1MB cap.
    image_history.append(model_turn)
