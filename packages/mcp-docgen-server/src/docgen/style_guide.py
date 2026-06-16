"""Opinionated style guide — 10 palettes, 8 font pairings, "don't" rules.

This is the highest-ROI block of the whole upgrade: even the best planner
output looks generic without it. SKILL.md mandates the specific choices
below (no accent lines, no centred body, plain bullets, DXA widths).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .ir import FontSpec, HexColor, Theme


@dataclass(frozen=True)
class Palette:
    name: str
    mood: Literal["professional", "bold", "warm", "cool", "muted", "festive", "editorial", "tech", "academic", "luxury"]
    hex_colors: tuple[str, ...]  # 5 entries: dark, primary, light, muted, accent

    def to_hexcolors(self) -> list[HexColor]:
        return [HexColor(value=h) for h in self.hex_colors]


@dataclass(frozen=True)
class FontPair:
    name: str
    heading: str
    body: str
    monospace: str = "Courier"


PALETTES: dict[str, Palette] = {
    "enterprise-green-gold": Palette("enterprise-green-gold", "luxury", ("0B3D2E", "C9A15A", "F5F1E8", "1F2937", "7C3AED")),
    "corporate-navy":    Palette("corporate-navy",    "professional", ("0F172A", "1E40AF", "F1F5F9", "64748B", "F59E0B")),
    "editorial-slate":   Palette("editorial-slate",   "editorial",    ("1F2937", "EF4444", "F9FAFB", "6B7280", "111827")),
    "tech-blue-white":   Palette("tech-blue-white",   "tech",         ("0D1B2A", "3B82F6", "FFFFFF", "CBD5E1", "22C55E")),
    "warm-terra":        Palette("warm-terra",        "warm",         ("3F2A14", "C2410C", "FEF3C7", "78350F", "EA580C")),
    "cool-mint":         Palette("cool-mint",         "cool",         ("064E3B", "10B981", "ECFDF5", "065F46", "0EA5E9")),
    "muted-paper":       Palette("muted-paper",       "muted",        ("2D2A26", "8B7355", "F4F1EA", "6B5F4F", "A0522D")),
    "bold-magenta":      Palette("bold-magenta",      "bold",         ("17063F", "D946EF", "FAF5FF", "581C87", "F472B6")),
    "academic-olive":    Palette("academic-olive",    "academic",     ("2A2A1F", "606C38", "FEFAE0", "BC6C25", "283618")),
    "festive-crimson":   Palette("festive-crimson",   "festive",      ("1A0A0A", "B91C1C", "FEF2F2", "991B1B", "F59E0B")),
}


FONT_PAIRS: dict[str, FontPair] = {
    "helvetica-helvetica":    FontPair("helvetica-helvetica",    "Helvetica-Bold", "Helvetica"),
    "helvetica-times":        FontPair("helvetica-times",        "Helvetica-Bold", "Times-Roman"),
    "times-helvetica":        FontPair("times-helvetica",        "Times-Bold",     "Helvetica"),
    "times-times":            FontPair("times-times",            "Times-Bold",     "Times-Roman"),
    "courier-helvetica":      FontPair("courier-helvetica",      "Courier-Bold",   "Helvetica"),
    "helvetica-courier":      FontPair("helvetica-courier",      "Helvetica-Bold", "Courier"),
    # The last two pairs require a Noto CJK / Inter install in the sandbox.
    # In the business container we degrade to Helvetica / Times.
    "inter-inter":            FontPair("inter-inter",            "Inter-Bold",     "Inter"),
    "notosans-notosans":      FontPair("notosans-notosans",      "NotoSans-Bold",  "NotoSans"),
}


DONT_RULES = (
    "Do not add an accent line under the title (Theme.accent_style='none').",
    "Do not centre body text — only titles may centre.",
    "Do not use Unicode pictogram bullets (◆◉★▶). Use '- ' or numbered lists.",
    "Do not prefix hex colours with '#' — 6 hex digits only.",
    "Do not use 8-digit hex (with alpha) in PPT. pptxgenjs rejects them.",
    "Do not insert pre-rasterised chart PNGs in PPT — use native chart shapes.",
    "Do not fabricate figures, dates, or named people you are not told.",
    "Do not leave lorem ipsum / TODO / xxxx placeholders in the final doc.",
    "Do not mix tracking units: use DXA for column widths, not percent.",
    "Do not skip alt_text on any image.",
)


def theme_from(palette_name: str, font_name: str, *, accent_style: str = "none") -> Theme:
    pal = PALETTES[palette_name]
    fp = FONT_PAIRS[font_name]
    return Theme(
        palette=pal.to_hexcolors(),
        font_primary=FontSpec(family=fp.body, size_pt=11.0),
        font_heading=FontSpec(family=fp.heading, size_pt=18.0, bold=True),
        accent_style=accent_style,  # type: ignore[arg-type]
    )


def default_theme() -> Theme:
    """The safest default — works cross-platform without extra fonts."""
    return theme_from("corporate-navy", "helvetica-helvetica", accent_style="none")


def prompt_style_block() -> str:
    """Text snippet for Planner system prompts — listing palettes + rules."""
    lines = ["# Style guide (obey without exception)"]
    lines.append("")
    lines.append("## Available palettes (pick one name; do not invent colours)")
    for name, p in PALETTES.items():
        lines.append(f"- {name} ({p.mood}): {', '.join(p.hex_colors)}")
    lines.append("")
    lines.append("## Available font pairs (heading / body)")
    for name, fp in FONT_PAIRS.items():
        lines.append(f"- {name}: {fp.heading} / {fp.body}")
    lines.append("")
    lines.append("## Absolute rules")
    for rule in DONT_RULES:
        lines.append(f"- {rule}")
    return "\n".join(lines)
