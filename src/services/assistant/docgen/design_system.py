"""Design systems — semantic design tokens for slides.

Rejects the ``palette[0..4]`` model in favour of semantic tokens
(surface / ink / accent / border / state) plus a modular typography
scale. This is the layer the renderer actually talks to.

Four built-in systems, each a complete design:

  stripe    — Stripe blog / docs look; ink #0A2540, accent #635BFF.
  carbon    — IBM Carbon productive, high-contrast, accent #0F62FE.
  keynote   — Apple Keynote-clean; surface white, accent #0071E3.
  editorial — magazine / New Yorker vibe, cream surface, red accent.
  hejaz     — project-specific green + gold (user's brand).

Each system supplies:
  * 14 colour tokens (semantic, LCH-ish derivation in mind)
  * 6 type tokens (eyebrow / display / h1 / h2 / lead / body / caption)
  * 3 font slots (display / body / mono)
  * 3 shape tokens (radius / shadow blur / shadow alpha)

Helpers:
  ``text_on(bg_hex)`` — WCAG-aware foreground picker.
  ``tint(hex, amount)`` — toward white.
  ``shade(hex, amount)`` — toward black.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# colour helpers


def _clamp(x: float, lo: float = 0.0, hi: float = 255.0) -> int:
    return int(max(lo, min(hi, x)))


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"{_clamp(r):02X}{_clamp(g):02X}{_clamp(b):02X}"


def tint(hex_value: str, amount: float) -> str:
    """Mix ``hex_value`` with white by ``amount`` (0..1)."""
    r, g, b = _hex_to_rgb(hex_value)
    return _rgb_to_hex(
        r + (255 - r) * amount,
        g + (255 - g) * amount,
        b + (255 - b) * amount,
    )


def shade(hex_value: str, amount: float) -> str:
    """Mix ``hex_value`` with black by ``amount`` (0..1)."""
    r, g, b = _hex_to_rgb(hex_value)
    return _rgb_to_hex(r * (1 - amount), g * (1 - amount), b * (1 - amount))


def _relative_luminance(hex_value: str) -> float:
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _hex_to_rgb(hex_value)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def text_on(bg_hex: str) -> str:
    """Return white or near-black based on background luminance."""
    return "FFFFFF" if _relative_luminance(bg_hex) < 0.45 else "0F172A"


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    lf = _relative_luminance(fg_hex) + 0.05
    lb = _relative_luminance(bg_hex) + 0.05
    return max(lf, lb) / min(lf, lb)


# ---------------------------------------------------------------------------
# typography


@dataclass(frozen=True)
class TypeScale:
    """Modular 1.333 scale, for 16:9 (13.33"×7.5") canvas.

    Sizes are in points. Weights map to CSS 100–900, python-pptx exposes
    ``font.bold`` only, so we collapse to bold ≥600.
    """

    eyebrow_pt: float = 11.0
    eyebrow_tracking_pct: float = 0.12     # ~120/1000em
    display_pt: float = 56.0                # hero title
    h1_pt: float = 36.0                     # slide title
    h2_pt: float = 24.0                     # section header inside slide
    lead_pt: float = 20.0                    # intro paragraph / deck
    body_pt: float = 16.0
    caption_pt: float = 12.0
    section_numeral_pt: float = 180.0        # oversized 01/08 decoration
    stat_numeral_pt: float = 176.0           # hero stat

    # Weights (python-pptx only cares about bold t/f but we keep the
    # semantic flag for future OOXML work).
    eyebrow_weight: int = 700
    display_weight: int = 800
    h1_weight: int = 700
    h2_weight: int = 700
    lead_weight: int = 400
    body_weight: int = 400


# ---------------------------------------------------------------------------
# colour tokens


@dataclass(frozen=True)
class ColorTokens:
    """Fourteen semantic colour tokens.

    Order ≠ priority — every renderer helper takes tokens by name, not index.
    """

    # Surfaces
    surface: str
    surface_elevated: str
    surface_inverted: str
    surface_tint: str

    # Ink (text)
    ink_primary: str
    ink_secondary: str
    ink_muted: str
    ink_inverted: str

    # Borders
    border_subtle: str
    border_strong: str

    # Accents (one brand + one secondary)
    accent: str
    accent_subtle: str
    accent_on: str
    accent_secondary: str


# ---------------------------------------------------------------------------
# shape tokens


@dataclass(frozen=True)
class ShapeTokens:
    radius_sm: float = 0.08          # Inches, for chips
    radius_md: float = 0.12          # for cards
    shadow_blur_emu: int = 40_000    # elevation
    shadow_dist_emu: int = 23_000
    shadow_dir_deg: int = 90         # shadow points down
    shadow_alpha_per_mille: int = 18_000  # 0..100000


# ---------------------------------------------------------------------------
# full design system


@dataclass(frozen=True)
class DesignSystem:
    name: str
    description: str
    colors: ColorTokens
    type_scale: TypeScale
    shapes: ShapeTokens
    font_display: str
    font_body: str
    font_mono: str = "Consolas"

    def slide_frame(self) -> tuple[float, float, float]:
        """(safe_left, safe_top, safe_margin) in inches."""
        return (0.66, 0.66, 0.66)


# ---------------------------------------------------------------------------
# built-in systems


def _stripe() -> DesignSystem:
    c = ColorTokens(
        surface="F6F9FC",
        surface_elevated="FFFFFF",
        surface_inverted="0A2540",
        surface_tint="EEF0FF",
        ink_primary="0A2540",
        ink_secondary="425466",
        ink_muted="8898AA",
        ink_inverted="FFFFFF",
        border_subtle="E3E8EE",
        border_strong="CFD7DF",
        accent="635BFF",
        accent_subtle="EEF0FF",
        accent_on="FFFFFF",
        accent_secondary="00D4FF",
    )
    return DesignSystem(
        name="stripe",
        description="Clean fintech editorial — Stripe blog / docs.",
        colors=c,
        type_scale=TypeScale(),
        shapes=ShapeTokens(),
        font_display="Calibri",
        font_body="Calibri",
    )


def _carbon() -> DesignSystem:
    c = ColorTokens(
        surface="FFFFFF",
        surface_elevated="F4F4F4",
        surface_inverted="161616",
        surface_tint="EDF5FF",
        ink_primary="161616",
        ink_secondary="393939",
        ink_muted="6F6F6F",
        ink_inverted="F4F4F4",
        border_subtle="E0E0E0",
        border_strong="8D8D8D",
        accent="0F62FE",
        accent_subtle="EDF5FF",
        accent_on="FFFFFF",
        accent_secondary="08BDBA",
    )
    return DesignSystem(
        name="carbon",
        description="IBM Carbon expressive — productive, high-contrast.",
        colors=c,
        type_scale=TypeScale(h1_pt=38.0),
        shapes=ShapeTokens(radius_sm=0.04, radius_md=0.04, shadow_blur_emu=30_000),
        font_display="Calibri",
        font_body="Calibri",
    )


def _keynote() -> DesignSystem:
    c = ColorTokens(
        surface="FFFFFF",
        surface_elevated="F5F5F7",
        surface_inverted="1D1D1F",
        surface_tint="E8F1FD",
        ink_primary="1D1D1F",
        ink_secondary="424245",
        ink_muted="86868B",
        ink_inverted="FFFFFF",
        border_subtle="D2D2D7",
        border_strong="86868B",
        accent="0071E3",
        accent_subtle="E8F1FD",
        accent_on="FFFFFF",
        accent_secondary="86868B",
    )
    return DesignSystem(
        name="keynote",
        description="Apple Keynote — spacious, neutral, SF Pro vibe.",
        colors=c,
        type_scale=TypeScale(display_pt=60.0, h1_pt=40.0, lead_pt=22.0),
        shapes=ShapeTokens(shadow_blur_emu=60_000, shadow_alpha_per_mille=12_000),
        font_display="Calibri",
        font_body="Calibri",
    )


def _editorial() -> DesignSystem:
    c = ColorTokens(
        surface="FAF8F4",
        surface_elevated="FFFFFF",
        surface_inverted="1B1B1B",
        surface_tint="FFE8EC",
        ink_primary="1B1B1B",
        ink_secondary="3F3F3F",
        ink_muted="807A6F",
        ink_inverted="FAF8F4",
        border_subtle="E4DFD4",
        border_strong="AFA796",
        accent="CF1F3A",
        accent_subtle="FFE8EC",
        accent_on="FFFFFF",
        accent_secondary="1B3A52",
    )
    return DesignSystem(
        name="editorial",
        description="Magazine / long-form — cream paper, red accent, serif.",
        colors=c,
        type_scale=TypeScale(display_pt=64.0, h1_pt=40.0, body_pt=17.0),
        shapes=ShapeTokens(radius_sm=0.02, radius_md=0.04),
        font_display="Times New Roman",
        font_body="Calibri",
    )


def _hejaz() -> DesignSystem:
    c = ColorTokens(
        surface="FAF8F2",
        surface_elevated="FFFFFF",
        surface_inverted="0B3D2E",
        surface_tint="F3EAD6",
        ink_primary="0B3D2E",
        ink_secondary="2B2B2B",
        ink_muted="7A7563",
        ink_inverted="FAF8F2",
        border_subtle="E4DECF",
        border_strong="C9A15A",
        accent="C9A15A",
        accent_subtle="F3EAD6",
        accent_on="0B3D2E",
        accent_secondary="7C3AED",
    )
    return DesignSystem(
        name="hejaz",
        description="Hejaz AI brand — deep green + gold.",
        colors=c,
        type_scale=TypeScale(),
        shapes=ShapeTokens(),
        font_display="Calibri",
        font_body="Calibri",
    )


_REGISTRY: dict[str, DesignSystem] = {
    "stripe": _stripe(),
    "carbon": _carbon(),
    "keynote": _keynote(),
    "editorial": _editorial(),
    "hejaz": _hejaz(),
}


def get_design_system(name: str) -> DesignSystem:
    if name not in _REGISTRY:
        raise KeyError(f"unknown design system: {name!r}; options: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def available_systems() -> list[str]:
    return sorted(_REGISTRY)


def default_design_system() -> DesignSystem:
    return _REGISTRY["stripe"]


# ---------------------------------------------------------------------------
# legacy-palette bridge


def design_system_from_palette(palette_hex: list[str]) -> DesignSystem:
    """Build an ad-hoc DesignSystem from the old 5-hex palette model.

    Legacy Theme objects still ship a 5-colour list. This function keeps
    the old style_guide.py path working while the caller can upgrade to
    named design systems.
    """
    if not palette_hex:
        return default_design_system()
    palette_hex = (palette_hex + palette_hex[-1:] * 5)[:5]
    dark, primary, light, muted, accent = palette_hex
    c = ColorTokens(
        surface=light,
        surface_elevated="FFFFFF",
        surface_inverted=dark,
        surface_tint=tint(primary, 0.88),
        ink_primary=dark,
        ink_secondary=shade(dark, 0.1) if _relative_luminance(dark) > 0.5 else tint(dark, 0.2),
        ink_muted=muted,
        ink_inverted="FFFFFF",
        border_subtle=tint(muted, 0.7),
        border_strong=muted,
        accent=primary,
        accent_subtle=tint(primary, 0.85),
        accent_on=text_on(primary),
        accent_secondary=accent,
    )
    return DesignSystem(
        name="custom",
        description="Ad-hoc system built from a 5-colour palette.",
        colors=c,
        type_scale=TypeScale(),
        shapes=ShapeTokens(),
        font_display="Calibri",
        font_body="Calibri",
    )
