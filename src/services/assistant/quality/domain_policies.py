from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ...knowledge.constants import ISLAMIC_SYNONYMS

_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


@dataclass(frozen=True)
class PolicyDecision:
    action: str  # "allow" | "decline"
    response: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ImamPolicyConfig:
    closing_phrase: str = (
        "All information provided is sourced from authenticated Islamic materials. "
        "For matters requiring personal guidance based on your specific circumstances, "
        "please consult with a qualified Islamic scholar."
    )
    decline_out_of_scope: str = (
        "I don't have sufficient information in my current knowledge base to provide "
        "a reliable answer to this question. I recommend consulting with a qualified "
        "Islamic scholar or visiting your local Islamic Centre for guidance on this matter."
    )
    decline_off_topic: str = (
        "I am designed to answer questions about Islamic knowledge. For other topics, "
        "please consult appropriate resources. Is there anything about Islam I can help you with?"
    )
    decline_forbidden: str = (
        "This question falls outside the permitted scope for this assistant. "
        "Please ask a question about Islamic knowledge covered in the provided materials."
    )
    # Confidence thresholds for Retrieval-First approach
    min_semantic_score: float = 0.35  # Minimum vector similarity score
    min_text_match: float = 0.15  # Minimum text match score (lowered from 0.18)


class ImamPolicy:
    """Policy engine for Wahda AI Imam assistant."""

    def __init__(self, config: ImamPolicyConfig | None = None) -> None:
        self.config = config or ImamPolicyConfig()
        self._islamic_terms = self._build_islamic_term_set()
        self._forbidden_patterns = self._build_forbidden_patterns()

    @staticmethod
    def _build_islamic_term_set() -> set[str]:
        base_terms = {
            "islam",
            "muslim",
            "quran",
            "qur'an",
            "hadith",
            "sunnah",
            "sharia",
            "fiqh",
            "aqeedah",
            "allah",
            "prophet",
            "muhammad",
            "imam",
            "madhhab",
            "hanafi",
            "maliki",
            "shafii",
            "hanbali",
            "ramadan",
            "zakat",
            "hajj",
            "umrah",
            "salah",
            "salat",
            "dua",
            "dhikr",
            "tawhid",
            "iman",
            "sabr",
            "halal",
            "haram",
        }
        for term, synonyms in ISLAMIC_SYNONYMS.items():
            base_terms.add(term.lower())
            for syn in synonyms:
                base_terms.add(str(syn).lower())
        return base_terms

    @staticmethod
    def _build_forbidden_patterns() -> list[re.Pattern[str]]:
        interfaith = (
            r"(compare|comparison|versus|vs\.?|difference between)"
            r".*\b(christian|christianity|judaism|jewish|hindu|hinduism|buddhism|sikh|sikhism)\b"
        )
        politics = (
            r"\b(politic|election|government|parliament|president|prime minister|party|vote)\b"
        )
        return [
            re.compile(interfaith, re.IGNORECASE),
            re.compile(politics, re.IGNORECASE),
        ]

    @staticmethod
    def _normalize_ws(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().lower()

    def scenario_rules(self) -> str:
        """Generate compact scenario rules for the system prompt.

        Only hard constraints that the gateway must enforce.
        Behavioral guidance (intent analysis, madhab depth, format adaptation)
        lives in the agent's own IMAM_SYSTEM_PROMPT — not here.
        """
        rules = [
            # 1. Identity override — gateway prompt says "Enterprise AI Assistant"
            "You are the AI Imam, a rigorous Islamic knowledge consultant for the Wahda community. "
            "Ground every answer in the authenticated knowledge base.",
            # 2. Source constraint [Imam.md §2-3]
            "Use ONLY the retrieved knowledge base content. NEVER supplement with external or general knowledge. "
            "If the KB does not cover the question, decline: state the topic is not in the current knowledge base.",
            # 3. Citation integrity [Imam.md §11-14] — binds to REF-N format from tool
            "When citing, use the exact Citation field from [REF-N] results. Do not construct, modify, or "
            "invent citations. Sort sources by authority: Quran > Hadith > Tafsir > Fiqh. "
            "End with a **Sources** list.",
            # 4. Closing phrase [Imam.md §23]
            f'End with the closing phrase exactly once: "{self.config.closing_phrase}"',
        ]
        return "<imam_rules>\n- " + "\n- ".join(rules) + "\n</imam_rules>"

    def should_apply(self, dataset: dict[str, Any]) -> bool:
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

    def precheck_query(self, query: str) -> PolicyDecision | None:
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
        contexts: Iterable[dict[str, Any]],
    ) -> PolicyDecision | None:
        """
        Retrieval-First confidence gating.

        Instead of using keyword matching to decide relevance, we let the knowledge
        base speak: if retrieval returns confident results, we answer; otherwise
        we politely decline. This follows Imam.md Section II Point 6:

        "If a question falls outside the knowledge base, the AI Imam should
        transparently inform the user that this specific topic is not covered
        in its current knowledge base"

        The confidence check uses both:
        - Semantic score (vector similarity) - captures meaning
        - Text match score - captures exact term overlap
        """
        # Convert to list to allow multiple iterations
        contexts_list = list(contexts)

        # No contexts = KB search returned nothing
        if not contexts_list:
            return PolicyDecision(
                action="decline",
                response=self._build_decline(self.config.decline_out_of_scope),
                reason="no_context",
            )

        # Extract all chunks and their scores
        all_chunks = [chunk for ctx in contexts_list for chunk in ctx.get("chunks", [])]

        if not all_chunks:
            return PolicyDecision(
                action="decline",
                response=self._build_decline(self.config.decline_out_of_scope),
                reason="empty_chunks",
            )

        # Calculate max semantic score (from vector search)
        max_semantic = max(
            (float(chunk.get("score") or 0.0) for chunk in all_chunks),
            default=0.0,
        )

        # Calculate max text match score
        max_text_match = max(
            (
                float((chunk.get("metadata") or {}).get("_text_match_score") or 0.0)
                for chunk in all_chunks
            ),
            default=0.0,
        )

        # Retrieval-First confidence check:
        # HIGH confidence (either score is strong) → allow
        # LOW confidence (both scores are weak) → decline
        #
        # This replaces keyword-based routing with confidence-based gating.
        # A query like "What is bismillah?" will pass if the KB returns
        # relevant content with good scores, even without keyword matching.

        if max_semantic >= self.config.min_semantic_score:
            return None  # Allow - good semantic match

        if max_text_match >= self.config.min_text_match:
            return None  # Allow - good text match

        # Low confidence - KB didn't find strong matches
        return PolicyDecision(
            action="decline",
            response=self._build_decline(self.config.decline_out_of_scope),
            reason="low_confidence",
        )

    def validate_answer(self, answer: str) -> list[str]:
        issues: list[str] = []
        if not answer or not answer.strip():
            issues.append("empty_answer")
            return issues

        normalized_answer = self._normalize_ws(answer)
        normalized_closing = self._normalize_ws(self.config.closing_phrase)

        # --- Structural checks ---

        closing_occurrences = normalized_answer.count(normalized_closing)
        if closing_occurrences == 0:
            issues.append("missing_closing_phrase")
        elif closing_occurrences > 1:
            issues.append("duplicate_closing_phrase")

        scholar_mentions = normalized_answer.count("qualified islamic scholar")
        if scholar_mentions > 1:
            issues.append("duplicate_consultation_reminder")

        # Require citation markers in content and a sources section
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

        # --- Quality checks ---

        # Citation mismatch: body [N] markers should map to Sources entries
        body_refs = set(re.findall(r"\[(\d+)\]", answer))
        sources_section = ""
        sources_match = re.search(
            r"(?:^\s*\*\*Sources\*?\*?|^\s*Sources:)(.*)",
            answer,
            re.MULTILINE | re.DOTALL,
        )
        if sources_match:
            sources_section = sources_match.group(1)
        sources_refs = set(re.findall(r"\[(\d+)\]", sources_section))
        # Check for body refs that have no corresponding source entry
        if body_refs and sources_section:
            orphan_body = body_refs - sources_refs
            if orphan_body:
                issues.append("citation_mismatch")

        # Generic answer: long response with no scholarly specificity
        word_count = len(answer.split())
        if word_count > 200:
            has_scholarly_ref = bool(
                re.search(
                    r"\b(hanafi|maliki|shafi.i|hanbali|ibn|al-|imam\s+\w+)\b",
                    answer,
                    re.IGNORECASE,
                )
            )
            if not has_scholarly_ref and not has_citation_marker:
                issues.append("generic_answer")

        return issues

    def build_repair_instructions(self, issues: list[str]) -> str:
        repairs = []
        if "missing_citations" in issues or "missing_sources_section" in issues:
            repairs.append(
                "Add citations after each paragraph and include a **Sources:** list at the end ordered by authority."
            )
        if "missing_closing_phrase" in issues:
            repairs.append(f'Append the exact closing phrase: "{self.config.closing_phrase}".')
        if "duplicate_closing_phrase" in issues or "duplicate_consultation_reminder" in issues:
            repairs.append(
                "Remove repeated advisory/disclaimer text. Keep only one consultation reminder by using "
                "the fixed closing phrase exactly once at the very end. Do not include an extra 'Important Note' "
                "that repeats the same reminder."
            )
        if "citation_mismatch" in issues:
            repairs.append(
                "Ensure every [N] marker in the text has a matching entry in the Sources section."
            )
        if "generic_answer" in issues:
            repairs.append(
                "Include specific madhab positions or scholarly references rather than generic frameworks."
            )
        if not repairs:
            repairs.append(
                "Ensure the response follows Imam requirements and uses only provided context."
            )
        return " ".join(repairs)

    def sanitize_answer(self, answer: str) -> str:
        """
        Normalize advisory content to avoid duplicated reminders.

        - Remove standalone "Important Note" advisory blocks that repeat scholar-consultation guidance.
        - Ensure the fixed closing phrase appears exactly once at the end.
        """
        if not answer or not answer.strip():
            return answer

        text = answer.strip()
        closing = self.config.closing_phrase.strip()

        # Remove redundant advisory paragraphs that duplicate the closing phrase intent.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        kept: list[str] = []
        for para in paragraphs:
            normalized = self._normalize_ws(para)
            is_duplicate_advisory = (
                "qualified islamic scholar" in normalized
                and (
                    "important note" in normalized
                    or "this answer addresses the general islamic principle" in normalized
                )
            )
            if is_duplicate_advisory:
                continue
            kept.append(para)

        body = "\n\n".join(kept).strip()

        # Remove all existing closing-phrase occurrences, then append exactly once.
        closing_pattern = re.compile(re.escape(closing), re.IGNORECASE)
        body = closing_pattern.sub("", body).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)

        if body:
            return f"{body}\n\n{closing}"
        return closing

    def _build_decline(self, reason_text: str) -> str:
        return f"{reason_text}\n\n{self.config.closing_phrase}"


class DomainPolicyResolver:
    """Resolve domain policy based on dataset metadata."""

    def __init__(self) -> None:
        self._imam_policy = ImamPolicy()

    def resolve(self, datasets: Iterable[dict[str, Any]]) -> ImamPolicy | None:
        for ds in datasets:
            if self._imam_policy.should_apply(ds):
                return self._imam_policy
        return None
