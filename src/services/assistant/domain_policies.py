from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from ..knowledge.constants import ISLAMIC_SYNONYMS


_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


@dataclass(frozen=True)
class PolicyDecision:
    action: str  # "allow" | "decline"
    response: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class ImamPolicyConfig:
    closing_phrase: str = (
        "All information provided is sourced from authenticated Islamic materials. "
        "For matters requiring personal guidance based on your specific circumstances, "
        "please consult with a qualified Islamic scholar."
    )
    decline_out_of_scope: str = (
        "The current knowledge base does not contain sufficient information to answer this question. "
        "Please consult a qualified Islamic scholar or a trusted Islamic centre for guidance."
    )
    decline_forbidden: str = (
        "This question falls outside the permitted scope for this assistant. "
        "Please ask a question about Islamic knowledge covered in the provided materials."
    )
    min_text_match: float = 0.18


class ImamPolicy:
    """Policy engine for Wahda AI Imam assistant."""

    def __init__(self, config: Optional[ImamPolicyConfig] = None) -> None:
        self.config = config or ImamPolicyConfig()
        self._islamic_terms = self._build_islamic_term_set()
        self._forbidden_patterns = self._build_forbidden_patterns()

    @staticmethod
    def _build_islamic_term_set() -> set[str]:
        base_terms = {
            "islam", "muslim", "quran", "qur'an", "hadith", "sunnah",
            "sharia", "fiqh", "aqeedah", "allah", "prophet", "muhammad",
            "imam", "madhhab", "hanafi", "maliki", "shafii", "hanbali",
            "ramadan", "zakat", "hajj", "umrah", "salah", "salat", "dua",
            "dhikr", "tawhid", "iman", "sabr", "halal", "haram",
        }
        for term, synonyms in ISLAMIC_SYNONYMS.items():
            base_terms.add(term.lower())
            for syn in synonyms:
                base_terms.add(str(syn).lower())
        return base_terms

    @staticmethod
    def _build_forbidden_patterns() -> List[re.Pattern[str]]:
        interfaith = (
            r"(compare|comparison|versus|vs\.?|difference between)"
            r".*\b(christian|christianity|judaism|jewish|hindu|hinduism|buddhism|sikh|sikhism)\b"
        )
        politics = r"\b(politic|election|government|parliament|president|prime minister|party|vote)\b"
        return [
            re.compile(interfaith, re.IGNORECASE),
            re.compile(politics, re.IGNORECASE),
        ]

    def scenario_rules(self) -> str:
        """Generate compact scenario rules for the system prompt."""
        rules = [
            "Only use the provided knowledge base content; do not add external knowledge.",
            "If the context is insufficient, decline with a clear reason.",
            "Use formal, objective, third-person language and avoid personal opinions.",
            "Avoid interfaith comparisons/criticism and political content.",
            "Provide citations after each paragraph and a sources list at the end.",
            f"End with the fixed closing phrase: \"{self.config.closing_phrase}\"",
        ]
        return "<imam_rules>\n- " + "\n- ".join(rules) + "\n</imam_rules>"

    def should_apply(self, dataset: Dict[str, Any]) -> bool:
        name = str(dataset.get("name") or "").lower()
        if "imam" in name:
            return True
        index_config = dataset.get("index_config")
        if isinstance(index_config, dict):
            retrieval = index_config.get("retrieval") or {}
            islamic = retrieval.get("islamic") if isinstance(retrieval, dict) else None
            if isinstance(islamic, dict) and any(islamic.values()):
                return True
        return False

    def _is_islamic_query(self, text: str) -> bool:
        if not text:
            return False
        if _ARABIC_PATTERN.search(text):
            return True
        lowered = text.lower()
        return any(term in lowered for term in self._islamic_terms)

    def _is_forbidden_query(self, text: str) -> bool:
        if not text:
            return False
        return any(pattern.search(text) for pattern in self._forbidden_patterns)

    def precheck_query(self, query: str) -> Optional[PolicyDecision]:
        if self._is_forbidden_query(query):
            return PolicyDecision(
                action="decline",
                response=self._build_decline(self.config.decline_forbidden),
                reason="forbidden_intent",
            )
        return None

    def precheck_context(
        self,
        query: str,
        contexts: Iterable[Dict[str, Any]],
    ) -> Optional[PolicyDecision]:
        if not contexts:
            return PolicyDecision(
                action="decline",
                response=self._build_decline(self.config.decline_out_of_scope),
                reason="no_context",
            )

        if self._is_islamic_query(query):
            return None

        max_match = max(
            (
                float((chunk.get("metadata") or {}).get("_text_match_score") or 0.0)
                for ctx in contexts
                for chunk in ctx.get("chunks", [])
            ),
            default=0.0,
        )

        if max_match < self.config.min_text_match:
            return PolicyDecision(
                action="decline",
                response=self._build_decline(self.config.decline_out_of_scope),
                reason="low_text_match",
            )
        return None

    def validate_answer(self, answer: str) -> List[str]:
        issues: List[str] = []
        if not answer or not answer.strip():
            issues.append("empty_answer")
            return issues

        if self.config.closing_phrase not in answer:
            issues.append("missing_closing_phrase")

        # Require citations markers in content and a sources section
        has_citation_marker = bool(re.search(r"\[[0-9]+\]", answer)) or bool(
            re.search(r"\b(Quran|Sahih|Tafsir|Hadith|Fiqh)\b", answer, re.IGNORECASE)
        )
        if not has_citation_marker:
            issues.append("missing_citations")

        has_sources = bool(re.search(r"^\s*\*\*Sources", answer, re.MULTILINE)) or bool(
            re.search(r"^\s*Sources:", answer, re.MULTILINE)
        )
        if not has_sources:
            issues.append("missing_sources_section")

        return issues

    def build_repair_instructions(self, issues: List[str]) -> str:
        repairs = []
        if "missing_citations" in issues or "missing_sources_section" in issues:
            repairs.append("Add citations after each paragraph and include a **Sources:** list at the end ordered by authority.")
        if "missing_closing_phrase" in issues:
            repairs.append(f'Append the exact closing phrase: "{self.config.closing_phrase}".')
        if not repairs:
            repairs.append("Ensure the response follows Imam requirements and uses only provided context.")
        return " ".join(repairs)

    def _build_decline(self, reason_text: str) -> str:
        return f"{reason_text}\n\n{self.config.closing_phrase}"


class DomainPolicyResolver:
    """Resolve domain policy based on dataset metadata."""

    def __init__(self) -> None:
        self._imam_policy = ImamPolicy()

    def resolve(self, datasets: Iterable[Dict[str, Any]]) -> Optional[ImamPolicy]:
        for ds in datasets:
            if self._imam_policy.should_apply(ds):
                return self._imam_policy
        return None
