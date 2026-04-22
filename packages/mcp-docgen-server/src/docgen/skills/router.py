"""Route user intent → Skill, then progressive-disclosure expand it.

Token budget (approximate; real tokenisation is left to the caller):

  - frontmatter only:  ~ 100 tok / skill
  - skill body:        ~ 500-3000 tok
  - sub-resource md:   ~ 500-3000 tok each (only loaded when referenced)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Skill
from .registry import SkillRegistry


@dataclass
class Intent:
    """Minimal intent signal for routing.

    ``doc_type`` is the strongest discriminator; ``keywords`` is a fallback
    used when a user says "make me a report" without specifying format.
    """

    doc_type: Optional[str] = None  # "docx" | "pptx" | "xlsx" | "pdf"
    keywords: list[str] | None = None


_KEYWORD_HINTS: dict[str, list[str]] = {
    "docx": ["word", "doc", "docx", "letter", "memo", "report", "article"],
    "pptx": ["ppt", "powerpoint", "slide", "deck", "presentation"],
    "xlsx": ["excel", "xls", "xlsx", "spreadsheet", "sheet", "model", "计算"],
    "pdf": ["pdf", "invoice", "certificate", "form", "brochure", "flyer"],
}


class SkillRouter:
    """Select a skill from the registry and expand with progressive disclosure."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def select(self, intent: Intent) -> Optional[Skill]:
        if intent.doc_type and intent.doc_type in self._registry:
            return self._registry.get(intent.doc_type)

        if intent.keywords:
            lowered = " ".join(k.lower() for k in intent.keywords)
            for doc_type, hints in _KEYWORD_HINTS.items():
                if any(h in lowered for h in hints) and doc_type in self._registry:
                    return self._registry.get(doc_type)
        return None

    def system_prompt_stub(self) -> str:
        """One-line-per-skill summary, safe to eager-load into system prompt."""
        lines = ["<available_skills>"]
        for fm in self._registry.frontmatters():
            # Cap the description so a huge one-line description cannot blow
            # up the system prompt.
            desc = fm.description.strip().split("\n")[0]
            if len(desc) > 240:
                desc = desc[:237] + "…"
            lines.append(f"  - {fm.name}: {desc}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def expand(
        self,
        skill: Skill,
        *,
        include_body: bool = True,
        include_resources: list[str] | None = None,
    ) -> str:
        """Return the full prompt-ready expansion of a skill.

        ``include_resources`` is a list of resource names (stem) that the
        caller wants eagerly loaded — e.g. ``["editing"]`` when we know we're
        going to edit an existing pptx.
        """
        parts: list[str] = []
        parts.append(f"# Skill: {skill.name}")
        parts.append(skill.description)
        if include_body and skill.body:
            parts.append("")
            parts.append(skill.body)
        if include_resources:
            for res_name in include_resources:
                r = skill.resource_by_name(res_name)
                if r is not None and r.path.is_file():
                    parts.append(f"\n## Resource: {res_name}.md\n")
                    parts.append(r.path.read_text(encoding="utf-8"))
        return "\n".join(parts)
