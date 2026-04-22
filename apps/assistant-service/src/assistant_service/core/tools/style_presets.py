"""
Image Style Presets — single source of truth for the public /generate-image API.

Exposes 9 user-facing styles plus a neutral default. Because Gemini and Doubao
have **no native style parameter**, style is realised via a curated English
prompt modifier injected into the user prompt. For DashScope we still map to
its native ``<style>`` tag and add a matching negative prompt.

This module is used by the public API layer ONLY. Legacy agent-internal tools
(``ImageGeneratorExecutor``) continue to accept DashScope-style tags unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StylePreset(str, Enum):
    """User-facing style presets accepted by /generate-image.

    Enum values are the wire-format strings the API and frontend exchange.
    Values keep the original naming intent ("3d_render" not "render_3d" etc.).
    """

    DEFAULT = "default"
    REALISTIC = "realistic"
    ANIME = "anime"
    ABSTRACT = "abstract"
    OIL_PAINT = "oil_paint"
    WATERCOLOR = "watercolor"
    RENDER_3D = "3d_render"
    PIXEL_ART = "pixel_art"
    SKETCH = "sketch"
    COMIC = "comic"


@dataclass(frozen=True)
class StyleDefinition:
    """Per-preset rendering recipe.

    ``prompt_modifier`` is an English descriptor appended to the user prompt for
    providers that lack a native style parameter (Gemini / Doubao). Writing
    style hints in English gives the most consistent results across models,
    regardless of the user's own prompt language.

    ``dashscope_tag`` is DashScope Wanx's native enum value. When we do not
    have a native equivalent (e.g. ``abstract``) we fall back to ``<auto>`` and
    still inject the modifier into the prompt as a hint.

    ``negative_prompt`` is a short exclusion list used only by DashScope, which
    has an explicit negative_prompt field.
    """

    prompt_modifier: str
    dashscope_tag: str
    negative_prompt: str = ""


#: Per-style recipes. English modifiers only (see class docstring).
STYLE_PRESETS: dict[StylePreset, StyleDefinition] = {
    StylePreset.DEFAULT: StyleDefinition(
        prompt_modifier="",
        dashscope_tag="<auto>",
        negative_prompt="",
    ),
    StylePreset.REALISTIC: StyleDefinition(
        prompt_modifier=(
            "photorealistic photograph, high detail, natural lighting, "
            "sharp focus, 35mm lens, shallow depth of field, true-to-life colors"
        ),
        dashscope_tag="<photography>",
        negative_prompt="cartoon, illustration, painting, sketch, anime",
    ),
    StylePreset.ANIME: StyleDefinition(
        prompt_modifier=(
            "anime illustration, cel-shaded, vibrant colors, expressive line work, "
            "clean linework, Japanese animation style"
        ),
        dashscope_tag="<anime>",
        negative_prompt="photorealistic, 3d render",
    ),
    StylePreset.ABSTRACT: StyleDefinition(
        prompt_modifier=(
            "abstract art, non-representational, bold geometric and organic shapes, "
            "expressive color fields, contemporary gallery aesthetic"
        ),
        dashscope_tag="<auto>",  # no native equivalent
        negative_prompt="photorealistic, literal representation",
    ),
    StylePreset.OIL_PAINT: StyleDefinition(
        prompt_modifier=(
            "oil painting, visible brushstrokes, impasto technique, rich textured canvas, "
            "classical composition, warm palette"
        ),
        dashscope_tag="<oil painting>",
        negative_prompt="digital art, smooth gradient, photograph",
    ),
    StylePreset.WATERCOLOR: StyleDefinition(
        prompt_modifier=(
            "watercolor painting, soft color washes, visible paper texture, "
            "bleeding edges, translucent layers, delicate brushwork"
        ),
        dashscope_tag="<watercolor>",
        negative_prompt="digital render, sharp edges, photograph",
    ),
    StylePreset.RENDER_3D: StyleDefinition(
        prompt_modifier=(
            "3D render, Octane/Blender style, volumetric lighting, subsurface scattering, "
            "high polygon detail, studio HDRI, cinematic composition"
        ),
        dashscope_tag="<3d cartoon>",
        negative_prompt="2d, flat, sketch, line art",
    ),
    StylePreset.PIXEL_ART: StyleDefinition(
        prompt_modifier=(
            "pixel art, 16-bit aesthetic, limited color palette, crisp pixel grid, "
            "retro video game style, dithering"
        ),
        dashscope_tag="<auto>",  # no native equivalent
        negative_prompt="smooth, photorealistic, anti-aliased",
    ),
    StylePreset.SKETCH: StyleDefinition(
        prompt_modifier=(
            "pencil sketch, graphite on paper, hatching and cross-hatching, "
            "expressive loose lines, monochrome tonal values"
        ),
        dashscope_tag="<sketch>",
        negative_prompt="color, photorealistic, painted",
    ),
    StylePreset.COMIC: StyleDefinition(
        prompt_modifier=(
            "comic book illustration, bold ink outlines, flat cel shading, "
            "halftone dot shading, dynamic panel composition, Western comic style"
        ),
        dashscope_tag="<auto>",  # no native equivalent
        negative_prompt="photorealistic, watercolor",
    ),
}


# ---------------------------------------------------------------------------
# Legacy string tolerance
# ---------------------------------------------------------------------------

#: Friendly-name and DashScope-tag aliases accepted as fallbacks. Lets us
#: upgrade callers without breaking older clients that still POST strings
#: like ``"oil"`` or ``"<photography>"``.
_LEGACY_ALIASES: dict[str, StylePreset] = {
    # Frontend pre-refactor friendly names
    "default": StylePreset.DEFAULT,
    "auto": StylePreset.DEFAULT,
    "photography": StylePreset.REALISTIC,
    "portrait": StylePreset.REALISTIC,
    "3d": StylePreset.RENDER_3D,
    "oil": StylePreset.OIL_PAINT,
    "flat": StylePreset.DEFAULT,
    # Raw DashScope tags (old API contract)
    "<auto>": StylePreset.DEFAULT,
    "<photography>": StylePreset.REALISTIC,
    "<portrait>": StylePreset.REALISTIC,
    "<anime>": StylePreset.ANIME,
    "<oil painting>": StylePreset.OIL_PAINT,
    "<watercolor>": StylePreset.WATERCOLOR,
    "<3d cartoon>": StylePreset.RENDER_3D,
    "<sketch>": StylePreset.SKETCH,
    "<flat illustration>": StylePreset.DEFAULT,
}


def resolve_style_preset(raw: str | StylePreset | None) -> StylePreset:
    """Tolerantly coerce a user-supplied style string into a ``StylePreset``.

    Order of resolution:
      1. ``None`` / empty → DEFAULT
      2. exact enum value match
      3. case-insensitive enum value match (``"Anime"`` → ANIME)
      4. case-insensitive legacy alias (``"oil"``, ``"<photography>"``)
      5. unknown → DEFAULT (no hard error — style is a hint, not security-sensitive)
    """
    if raw is None:
        return StylePreset.DEFAULT
    if isinstance(raw, StylePreset):
        return raw
    s = raw.strip()
    if not s:
        return StylePreset.DEFAULT
    try:
        return StylePreset(s)
    except ValueError:
        pass
    s_lower = s.lower()
    try:
        return StylePreset(s_lower)
    except ValueError:
        pass
    return _LEGACY_ALIASES.get(s_lower, StylePreset.DEFAULT)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def compose_styled_prompt(user_prompt: str, preset: StylePreset) -> str:
    """Append the preset's modifier to the user's prompt.

    Used for providers without a native style param (Gemini, Doubao). The
    modifier is placed AFTER the subject so the user's content dominates, but
    clearly marked as a Style: section so it receives high weight in the
    model's interpretation.
    """
    definition = STYLE_PRESETS[preset]
    modifier = definition.prompt_modifier
    if not modifier:
        return user_prompt
    prompt = (user_prompt or "").rstrip()
    # Avoid double-injection if the caller has already composed the prompt
    # (e.g. multi-turn flows that re-use prior turn text).
    if modifier.lower() in prompt.lower():
        return prompt
    separator = "" if prompt.endswith((".", "!", "?")) else "."
    return f"{prompt}{separator} Style: {modifier}."


def resolve_dashscope_style_tag(preset: StylePreset) -> str:
    """Return the DashScope Wanx ``<style>`` tag for this preset."""
    return STYLE_PRESETS[preset].dashscope_tag


def resolve_negative_prompt(preset: StylePreset) -> str:
    """Return the DashScope negative prompt for this preset (may be empty)."""
    return STYLE_PRESETS[preset].negative_prompt


def needs_prompt_injection_for_dashscope(preset: StylePreset) -> bool:
    """True when DashScope lacks a native tag for this preset.

    In that case we also inject the modifier into the prompt so the tag=<auto>
    call still produces a stylistically relevant image.
    """
    return STYLE_PRESETS[preset].dashscope_tag == "<auto>" and preset is not StylePreset.DEFAULT
