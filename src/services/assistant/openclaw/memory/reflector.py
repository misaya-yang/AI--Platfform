"""Daily reflection helper for memory maintenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


@dataclass
class ReflectionResult:
    """Generated reflection summary and extracted facts."""

    summary: str
    facts: list[str]
    reflection_date: date


class DailyMemoryReflector:
    """Generate compact daily reflection from recent session messages."""

    def build_reflection(
        self,
        *,
        messages: list[dict[str, Any]],
        reflection_date: date | None = None,
        max_facts: int = 8,
    ) -> ReflectionResult:
        day = reflection_date or datetime.now(timezone.utc).date()

        user_points: list[str] = []
        assistant_points: list[str] = []
        for msg in messages[-40:]:
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            sentence = content.replace("\n", " ").strip()
            if len(sentence) > 200:
                sentence = sentence[:197] + "..."
            if role == "user":
                user_points.append(sentence)
            elif role == "assistant":
                assistant_points.append(sentence)

        facts: list[str] = []
        for point in user_points:
            lowered = point.lower()
            if any(marker in lowered for marker in ("prefer", "喜欢", "习惯", "always", "usually")):
                facts.append(point)
            if len(facts) >= max_facts:
                break

        if not facts:
            facts = user_points[: max_facts // 2]

        summary_lines = [
            f"Daily reflection for {day.isoformat()}",
            f"- User messages analyzed: {len(user_points)}",
            f"- Assistant messages analyzed: {len(assistant_points)}",
        ]
        if facts:
            summary_lines.append("- Candidate long-term facts:")
            for fact in facts[:max_facts]:
                summary_lines.append(f"  - {fact}")

        return ReflectionResult(
            summary="\n".join(summary_lines),
            facts=facts[:max_facts],
            reflection_date=day,
        )
