from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PolicyDecision:
    action: str  # "allow" | "decline"
    response: str | None = None
    reason: str | None = None


class DomainPolicy:
    """Base interface for optional assistant domain policies.

    The open-source assistant ships without a built-in domain-specific policy.
    Projects can provide their own resolver implementation via dependency
    injection when they need stricter dataset or answer rules.
    """

    def scenario_rules(self) -> str:
        return ""

    def precheck_query(self, query: str) -> PolicyDecision | None:
        del query
        return None

    def precheck_context(
        self,
        query: str,
        contexts: Iterable[dict[str, Any]],
    ) -> PolicyDecision | None:
        del query, contexts
        return None

    def sanitize_answer(self, text: str) -> str:
        return text

    def validate_answer(self, text: str) -> list[str]:
        del text
        return []

    def build_repair_instructions(self, issues: Iterable[str]) -> str:
        return "\n".join(f"- {issue}" for issue in issues)


class DomainPolicyResolver:
    """Default no-op resolver for generic assistant deployments."""

    def resolve(self, datasets: Iterable[dict[str, Any]]) -> DomainPolicy | None:
        del datasets
        return None
