"""Layout rule engine for PPTX post-LLM uplifts.

Each rule is a pure function on ``list[PptxSlide]`` returning a new list.
Rules compose in a fixed order that matters:

  1. ``ComparisonTableRule`` / ``PullQuoteCardRule`` / ``BigStatRule`` —
     content-driven uplifts that change individual slide layouts.
  2. ``LayoutVarietyRule`` — breaks up consecutive same-layout runs.
  3. ``SectionDividerRule`` — inserts banner slides between parts.
  4. ``DarkCardInterleaveRule`` — tonal variety pass.
  5. ``EyebrowHintRule`` — writes running-header metadata to notes.

Keep rules single-responsibility: if a rule needs to read another rule's
private state, that's a sign the ordering (or the rule) is wrong.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ..ir import (
    BulletBlock,
    HeadingBlock,
    ParagraphBlock,
    PptxSlide,
    QuoteBlock,
)
from .base import Brief


class LayoutRule(ABC):
    """A rule that rewrites a slide list. Must be pure (no mutation)."""

    name: str = ""

    @abstractmethod
    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        """Return a NEW list; never mutate ``slides`` or the slide objects."""


# ---------------------------------------------------------------------------
# Content-driven uplifts (one slide at a time)

_LEVEL_SPLIT_RE = re.compile(r"(?:Level|级别|等级|L)\s*([1-9])\b", re.IGNORECASE)


def _split_levels(text: str) -> list[str]:
    """Split 'Level 1 foo. Level 2 bar.' → ['L1: foo', 'L2: bar']."""
    matches = list(_LEVEL_SPLIT_RE.finditer(text))
    if len(matches) < 2:
        return []
    out = []
    for i, m in enumerate(matches):
        n = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = re.sub(r"^[. ：:—-]+|[. ：:—-]+$", "", text[start:end])
        if body:
            out.append(f"L{n}: {body}")
    return out


class ComparisonTableRule(LayoutRule):
    """Title matches /maturity|comparison|levels|tiers|matrix/ AND body has
    ``Label: description``-shaped items → ``comparison_table``."""

    name = "comparison_table"
    _TITLE_KEYS = ("maturity", "comparison", "levels", "tiers", "matrix")

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        return [self._rewrite(s) for s in slides]

    def _rewrite(self, s: PptxSlide) -> PptxSlide:
        if s.layout == "title":
            return s
        title_l = (s.title or "").lower()
        if not any(k in title_l for k in self._TITLE_KEYS):
            return s
        bullet_items: list[str] = []
        keep: list = []
        for b in s.body or []:
            if isinstance(b, BulletBlock):
                bullet_items.extend(b.items)
            elif isinstance(b, ParagraphBlock):
                split_items = _split_levels(b.text)
                if split_items:
                    bullet_items.extend(split_items)
                elif ":" in b.text or "：" in b.text or " — " in b.text:
                    bullet_items.append(b.text)
                else:
                    keep.append(b)
            elif isinstance(b, HeadingBlock):
                if ":" in b.text or "：" in b.text or " — " in b.text:
                    bullet_items.append(b.text)
                else:
                    keep.append(b)
            else:
                keep.append(b)
        table_ready = [it for it in bullet_items if (":" in it or "：" in it or " — " in it)]
        if not table_ready:
            return s
        return s.model_copy(update={
            "layout": "comparison_table",
            "body": [BulletBlock(items=table_ready)],
        })


class PullQuoteCardRule(LayoutRule):
    """Body contains a ``QuoteBlock`` with author → ``pull_quote_card``."""

    name = "pull_quote_card"

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        out = []
        for s in slides:
            if s.layout == "title":
                out.append(s)
                continue
            if any(isinstance(b, QuoteBlock) and b.author for b in (s.body or [])):
                out.append(s.model_copy(update={"layout": "pull_quote_card"}))
            else:
                out.append(s)
        return out


# 4-digit years 1900-2099 — reject UNLESS followed by a unit token.
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_UNIT_RE = re.compile(r"[%xX×kKMm]|pp|bp|PRs")
_NUMBER_RE = re.compile(r"(?:^|\s)(−?-?\d{1,5}(?:,\d{3})*(?:\.\d+)?\s*(?:%|x|×|PRs|bp|k|K|M|pp)?)(?:\s|$|[.,])")


def extract_big_number(text: str) -> str | None:
    """Extract the first 'big' number — 2+ digits or explicit unit suffix.

    Excludes 4-digit years (1900-2099) unless the number carries a unit.
    """
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    tok = m.group(1).strip()
    digits = re.sub(r"[^\d]", "", tok)
    has_unit = bool(_UNIT_RE.search(tok))
    # Year-exclusion: bare 4-digit 19xx/20xx with no unit → reject.
    if not has_unit and _YEAR_RE.match(digits):
        return None
    if len(digits) >= 2 or has_unit:
        return tok
    return None


class BigStatRule(LayoutRule):
    """Short body with a paragraph containing a dominant number → ``big_stat``.

    Skips boilerplate slides (references, agenda, etc.) where a random
    year would hijack the stat.

    Preserves surrounding body blocks: only the paragraph that yielded the
    stat is consumed (into stat_value + stat_label); other blocks remain.
    """

    name = "big_stat"
    _SKIP_KEYS = (
        "reference", "citation", "source", "thanks", "thank you",
        "appendix", "credits", "table of contents", "agenda",
    )

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        return [self._rewrite(s) for s in slides]

    def _rewrite(self, s: PptxSlide) -> PptxSlide:
        if s.layout == "title":
            return s
        title_l = (s.title or "").lower()
        if any(k in title_l for k in self._SKIP_KEYS):
            return s
        body = s.body or []
        if len(body) > 2:
            return s
        # Find the first paragraph that yields a stat.
        for idx, b in enumerate(body):
            if not isinstance(b, ParagraphBlock):
                continue
            stat = extract_big_number(b.text)
            if stat is None:
                continue
            rest = b.text.replace(stat, "", 1).strip(" -—:;,.")
            new_label = rest[:160] if rest else s.stat_label
            # Preserve any OTHER body blocks (don't wipe list).
            remaining = [x for i, x in enumerate(body) if i != idx]
            # If the sole body block was this paragraph, body becomes [] —
            # big_stat renderer ignores body anyway in that canonical case.
            return s.model_copy(update={
                "layout": "big_stat",
                "stat_value": stat,
                "stat_label": new_label,
                "body": remaining,
            })
        return s


# ---------------------------------------------------------------------------
# Variety pass

_LAYOUT_ALTERNATIVES: dict[str, list[str]] = {
    "title_content": ["halfbleed_image", "two_col", "grid_2x2", "title_content"],
    "grid_2x2": ["icon_row", "title_content", "grid_2x2"],
    "icon_row": ["grid_2x2", "title_content", "icon_row"],
    "two_col": ["halfbleed_image", "title_content", "two_col"],
    "halfbleed_image": ["two_col", "title_content", "halfbleed_image"],
}


def _layout_fits_content(layout: str, s: PptxSlide) -> bool:
    body = s.body or []
    if layout in ("title_content", "halfbleed_image"):
        return True
    content_units = 0
    for b in body:
        if isinstance(b, BulletBlock):
            content_units += len(b.items)
        else:
            content_units += 1
    if layout in ("grid_2x2", "icon_row"):
        return content_units >= 3
    if layout == "two_col":
        return content_units >= 2
    return True


class LayoutVarietyRule(LayoutRule):
    """If two consecutive slides share a layout, swap the second to a
    least-recently-used alternative that still fits its content shape."""

    name = "layout_variety"

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        if len(slides) <= 2:
            return list(slides)
        prev = slides[0].layout
        out = [slides[0]]
        last_used: dict[str, int] = {slides[0].layout: 0}
        for idx, s in enumerate(slides[1:], start=1):
            if s.layout == prev and s.layout in _LAYOUT_ALTERNATIVES:
                candidates = [
                    a for a in _LAYOUT_ALTERNATIVES[s.layout]
                    if a != prev and _layout_fits_content(a, s)
                ]
                if candidates:
                    best = max(candidates, key=lambda a: idx - last_used.get(a, -10))
                    s = s.model_copy(update={"layout": best})
            out.append(s)
            last_used[s.layout] = idx
            prev = s.layout
        return out


# ---------------------------------------------------------------------------
# Section dividers (with Chinese keyword support — B1)

# Patterns for the three structural boundaries.
_LAYER_PATTERNS = [
    re.compile(r"\blayer\s*1\b", re.IGNORECASE),
    re.compile(r"第\s*一\s*层"),
    re.compile(r"第\s*1\s*层"),
    re.compile(r"层\s*1\b"),
]
_MATURITY_KEYS = (
    "maturity", "levels", "tiers",
    "成熟度", "阶段", "层级",
)
_CLOSE_KEYS = (
    "closing", "close", "questions", "thanks", "references",
    "结束语", "参考资料", "引用", "致谢",
)


def _matches_layer(title: str) -> bool:
    return any(p.search(title) for p in _LAYER_PATTERNS)


def _matches_any(title_lower: str, title: str, keys: tuple[str, ...]) -> bool:
    # ASCII keys use case-insensitive match on title_lower; CJK keys use
    # raw title (case has no meaning for CJK).
    for k in keys:
        if k.isascii():
            if k.lower() in title_lower:
                return True
        else:
            if k in title:
                return True
    return False


class SectionDividerRule(LayoutRule):
    """Insert ``section_divider`` slides before Layer-1 / Maturity / Close
    boundaries. Supports English and Chinese boundary keywords."""

    name = "section_divider"

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        if not slides:
            return list(slides)
        layer_idx: int | None = None
        maturity_idx: int | None = None
        close_idx: int | None = None
        for i, s in enumerate(slides):
            title = s.title or ""
            t_low = title.lower()
            if layer_idx is None and _matches_layer(title):
                layer_idx = i
            if maturity_idx is None and _matches_any(t_low, title, _MATURITY_KEYS):
                maturity_idx = i
            if close_idx is None and _matches_any(t_low, title, _CLOSE_KEYS):
                close_idx = i

        inserts: dict[int, tuple[str, str, int, int]] = {}
        total_parts = sum(1 for x in (layer_idx, maturity_idx, close_idx) if x is not None)
        if total_parts == 0:
            return list(slides)
        part_num = 0
        if layer_idx is not None:
            part_num += 1
            inserts[layer_idx] = ("The Six Layers", "PART", part_num, total_parts)
        if maturity_idx is not None:
            part_num += 1
            inserts[maturity_idx] = ("Maturity & Roadmap", "PART", part_num, total_parts)
        if close_idx is not None:
            part_num += 1
            inserts[close_idx] = ("Closing & References", "PART", part_num, total_parts)

        out: list[PptxSlide] = []
        for i, s in enumerate(slides):
            if i in inserts:
                title, eyebrow, pn, tp = inserts[i]
                out.append(PptxSlide(
                    layout="section_divider",
                    title=title,
                    subtitle=eyebrow,
                    notes=f"PART: {pn}/{tp}",
                ))
            out.append(s)
        return out


# ---------------------------------------------------------------------------
# Dark-card interleave

class DarkCardInterleaveRule(LayoutRule):
    """Convert every ~3rd eligible title_content slide into ``dark_card``
    for tonal variety. Skips layouts that already have their own treatment.
    """

    name = "dark_card_interleave"
    _SKIP = frozenset({
        "title", "section_divider", "quote", "pull_quote_card",
        "halfbleed_image", "big_stat", "dark_card", "comparison_table",
        "chart",
    })

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        del brief
        eligible = [i for i, s in enumerate(slides) if s.layout not in self._SKIP]
        flip: set[int] = set()
        for k in (1, 4, 7):
            if k < len(eligible):
                flip.add(eligible[k])
        return [
            s.model_copy(update={"layout": "dark_card"}) if i in flip else s
            for i, s in enumerate(slides)
        ]


# ---------------------------------------------------------------------------
# Eyebrow hints

_FILLER_WORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "with",
    "on", "in", "at", "by", "from", "is", "are", "was", "be",
    "this", "that", "these", "those", "its", "deck", "slide",
    "slides", "presentation",
}


def _derive_eyebrow(brief: Brief) -> str:
    def _pick(text: str) -> str:
        tokens = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", text)
        kept = [t for t in tokens if t.lower() not in _FILLER_WORDS]
        if not kept:
            return ""
        candidate = " ".join(kept[:4])
        if len(candidate) > 28:
            candidate = " ".join(kept[:3])
        if len(candidate) > 28:
            candidate = candidate[:28].rstrip()
        return candidate.upper()

    goal_eyebrow = _pick(brief.goal)
    if goal_eyebrow:
        return goal_eyebrow
    return _pick(brief.title) or "SECTION"


class EyebrowHintRule(LayoutRule):
    """Write ``EYEBROW: <kicker>`` into slide notes so the renderer can
    use it as a running header above each H1."""

    name = "eyebrow_hint"

    def apply(self, slides: list[PptxSlide], brief: Brief) -> list[PptxSlide]:
        kicker_default = brief.style_hints.get("eyebrow") or _derive_eyebrow(brief)
        out = []
        for s in slides:
            if s.layout == "title":
                out.append(s)
                continue
            existing = s.notes or ""
            if "EYEBROW:" in existing:
                out.append(s)
                continue
            if not kicker_default:
                out.append(s)
                continue
            new_notes = f"EYEBROW: {kicker_default}\n{existing}".strip()
            out.append(s.model_copy(update={"notes": new_notes}))
        return out


# ---------------------------------------------------------------------------
# Pipeline

# Order matters — don't reorder without understanding dependencies.
#   * Content-driven uplifts FIRST so variety/dividers see final layouts.
#   * Variety before section-divider so section_divider slots aren't
#     counted as same-layout neighbours.
#   * Eyebrow LAST so it runs after all layout churn.
DEFAULT_RULES: list[LayoutRule] = [
    ComparisonTableRule(),
    PullQuoteCardRule(),
    BigStatRule(),
    LayoutVarietyRule(),
    SectionDividerRule(),
    DarkCardInterleaveRule(),
    EyebrowHintRule(),
]


def apply_rules(
    slides: list[PptxSlide],
    brief: Brief,
    rules: list[LayoutRule] | None = None,
) -> list[PptxSlide]:
    """Run each rule in order. Each rule returns a new list."""
    if rules is None:
        rules = DEFAULT_RULES
    current = list(slides)
    for r in rules:
        current = r.apply(current, brief)
    return current
