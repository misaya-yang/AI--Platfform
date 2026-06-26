"""Context assembler V2 with deterministic cost attribution."""

from __future__ import annotations

from typing import Any

from ...rag.context_engine import ContextBudgetManager, ContextEngine, ContextStructure
from .cost_breakdown import ContextCostBreakdown


class ContextAssemblerV2:
    """Compose messages and detailed cost output for model calls."""

    def __init__(
        self,
        *,
        provider: str,
        budget_manager: ContextBudgetManager | None = None,
        context_engine: ContextEngine | None = None,
        cost_breakdown: ContextCostBreakdown | None = None,
    ) -> None:
        self.provider = provider
        self.budget_manager = budget_manager or ContextBudgetManager()
        self.context_engine = context_engine or ContextEngine(provider=provider)
        self.cost_breakdown = cost_breakdown or ContextCostBreakdown()

    def build(
        self,
        *,
        context: ContextStructure,
        model_context_window: int,
        tool_definitions: list[dict[str, Any]] | None = None,
        injected_files: list[dict[str, Any]] | None = None,
        skills_metadata: list[dict[str, Any]] | None = None,
        memory_snippets: list[str] | None = None,
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
        artifact_summaries: list[dict[str, Any] | str] | None = None,
        compaction_summary: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Build model messages, budget plan, and context cost detail."""
        context.current_context = self._compose_request_context(
            current_context=context.current_context,
            source_summaries=source_summaries,
            tool_result_summaries=tool_result_summaries,
            artifact_summaries=artifact_summaries,
            compaction_summary=compaction_summary,
        )
        plan = self.budget_manager.create_plan(
            context=context,
            model_context_window=model_context_window,
        )
        context.conversation_history = plan.trimmed_history
        context.tool_definitions = tool_definitions or context.tool_definitions

        messages = self.context_engine.build_messages(context)
        detail = self.cost_breakdown.analyze(
            system_prompt=context.system_prompt,
            messages=messages,
            tool_definitions=context.tool_definitions,
            injected_files=injected_files,
            skills_metadata=skills_metadata,
            memory_snippets=memory_snippets,
            source_summaries=source_summaries,
            tool_result_summaries=tool_result_summaries,
            artifact_summaries=artifact_summaries,
            compaction_summary=compaction_summary,
        )

        return messages, plan.to_budget_event(), detail

    @classmethod
    def _compose_request_context(
        cls,
        *,
        current_context: str | None,
        source_summaries: list[dict[str, Any] | str] | None,
        tool_result_summaries: list[dict[str, Any] | str] | None,
        artifact_summaries: list[dict[str, Any] | str] | None,
        compaction_summary: str | None,
    ) -> str | None:
        """Append bounded request-scoped summaries in context-packet order."""
        sections: list[str] = []
        if current_context and current_context.strip():
            sections.append(current_context.strip())

        for heading, items in (
            ("Source Summaries", source_summaries),
            ("Recent Tool Results", tool_result_summaries),
            ("Recent Artifacts", artifact_summaries),
        ):
            section = cls._format_summary_section(heading, items)
            if section:
                sections.append(section)

        if compaction_summary and compaction_summary.strip():
            sections.append(
                "## Compaction Summary\n"
                + cls._bounded_text(compaction_summary, max_chars=600)
            )

        return "\n\n".join(sections) if sections else current_context

    @classmethod
    def _format_summary_section(
        cls,
        heading: str,
        items: list[dict[str, Any] | str] | None,
        *,
        max_items: int = 5,
    ) -> str:
        if not items:
            return ""
        lines = [
            f"- {cls._bounded_text(cls._summary_item_text(item), max_chars=360)}"
            for item in items[:max_items]
        ]
        return f"## {heading}\n" + "\n".join(lines)

    @staticmethod
    def _summary_item_text(item: dict[str, Any] | str) -> str:
        if isinstance(item, str):
            return item
        keys = (
            "source_type",
            "citation",
            "freshness",
            "tool_name",
            "artifact_id",
            "name",
            "title",
            "summary",
            "content",
        )
        parts = [f"{key}: {item[key]}" for key in keys if item.get(key)]
        return "; ".join(parts) if parts else str(item)

    @staticmethod
    def _bounded_text(value: Any, *, max_chars: int) -> str:
        text = str(value).replace("\n", " ").strip()
        if len(text) > max_chars:
            return f"{text[: max_chars - 3]}..."
        return text
