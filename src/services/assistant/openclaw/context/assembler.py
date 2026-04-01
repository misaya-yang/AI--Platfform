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
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Build model messages, budget plan, and context cost detail."""
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
        )

        return messages, plan.to_budget_event(), detail
