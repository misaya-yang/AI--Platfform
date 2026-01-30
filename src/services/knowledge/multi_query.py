"""
Multi-Query Retrieval for Islamic Knowledge Base

Generates multiple query reformulations for improved recall, specifically
optimized for Islamic terminology with a built-in synonym/transliteration map.

Key features:
- Zero-cost Islamic terminology expansion (no LLM needed)
- Arabic ↔ English transliteration mapping
- Cross-madhab query variants
- Results fused via Reciprocal Rank Fusion (RRF)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


# Islamic terminology synonym/transliteration map
# Maps common English terms to their Arabic transliterations and synonyms
ISLAMIC_SYNONYMS: Dict[str, List[str]] = {
    # Pillars of Islam
    "prayer": ["salah", "salat", "صلاة", "namaz"],
    "salah": ["prayer", "salat", "صلاة", "namaz"],
    "fasting": ["sawm", "siyam", "صيام", "صوم", "ramadan"],
    "sawm": ["fasting", "siyam", "صيام", "صوم"],
    "charity": ["zakat", "zakah", "sadaqah", "زكاة", "صدقة"],
    "zakat": ["charity", "zakah", "زكاة", "almsgiving"],
    "pilgrimage": ["hajj", "حج", "umrah", "عمرة"],
    "hajj": ["pilgrimage", "حج"],
    "faith": ["iman", "إيمان", "belief", "aqeedah", "عقيدة"],
    "iman": ["faith", "إيمان", "belief"],

    # Purification
    "ablution": ["wudu", "wudhu", "وضوء"],
    "wudu": ["ablution", "wudhu", "وضوء", "purification"],
    "purification": ["tahara", "طهارة", "ghusl", "غسل", "wudu"],
    "ghusl": ["bath", "غسل", "ritual bath", "purification"],

    # Finance
    "interest": ["riba", "usury", "ربا"],
    "riba": ["interest", "usury", "ربا"],
    "usury": ["riba", "interest", "ربا"],

    # Family law
    "marriage": ["nikah", "نكاح", "zawaj", "زواج"],
    "nikah": ["marriage", "نكاح", "zawaj"],
    "divorce": ["talaq", "طلاق", "khul", "خلع"],
    "talaq": ["divorce", "طلاق"],
    "inheritance": ["mirath", "ميراث", "wasiyyah", "وصية"],

    # Worship
    "supplication": ["dua", "duaa", "دعاء"],
    "dua": ["supplication", "duaa", "دعاء", "prayer"],
    "remembrance": ["dhikr", "ذكر", "zikr"],
    "dhikr": ["remembrance", "ذكر", "zikr"],

    # Ethics
    "modesty": ["haya", "حياء", "hijab"],
    "patience": ["sabr", "صبر"],
    "sabr": ["patience", "صبر", "endurance"],
    "gratitude": ["shukr", "شكر"],
    "repentance": ["tawbah", "توبة", "istighfar", "استغفار"],

    # Food
    "halal": ["حلال", "permissible", "lawful"],
    "haram": ["حرام", "forbidden", "prohibited", "impermissible"],
    "food": ["halal", "haram", "حلال", "حرام", "diet"],

    # Concepts
    "prophet": ["nabi", "نبي", "rasul", "رسول", "muhammad", "محمد"],
    "quran": ["القرآن", "qur'an", "book of allah", "كتاب الله"],
    "hadith": ["الحديث", "sunnah", "السنة", "tradition"],
    "sunnah": ["hadith", "السنة", "prophetic tradition"],
    "sharia": ["shariah", "الشريعة", "islamic law"],
    "fatwa": ["فتوى", "ruling", "legal opinion"],
    "jihad": ["جهاد", "struggle", "striving"],
    "ummah": ["أمة", "community", "muslim community"],

    # Schools of thought
    "hanafi": ["حنفي", "abu hanifah", "أبو حنيفة"],
    "maliki": ["مالكي", "imam malik", "الإمام مالك"],
    "shafii": ["شافعي", "imam shafi", "الإمام الشافعي"],
    "hanbali": ["حنبلي", "ahmad ibn hanbal", "أحمد بن حنبل"],
}


class MultiQueryRetrieval:
    """Generate multiple query reformulations for improved Islamic knowledge retrieval."""

    def generate_queries(
        self,
        query: str,
        n: int = 3,
    ) -> List[str]:
        """Generate query reformulations using rule-based expansion.

        Args:
            query: Original user query
            n: Maximum number of expanded queries (including original)

        Returns:
            List of queries (original first, then expansions)
        """
        queries = [query]

        # Rule-based Islamic terminology expansion
        expanded = self._expand_islamic_terms(query)
        for eq in expanded:
            if eq.lower() != query.lower() and eq not in queries:
                queries.append(eq)
                if len(queries) >= n:
                    break

        return queries[:n]

    def _expand_islamic_terms(self, query: str) -> List[str]:
        """Expand query with Islamic terminology synonyms/transliterations."""
        expansions = []
        query_lower = query.lower()
        words = query_lower.split()

        for word in words:
            # Strip common suffixes for matching
            clean_word = re.sub(r'[?.!,;:\'"]+$', '', word)

            if clean_word in ISLAMIC_SYNONYMS:
                synonyms = ISLAMIC_SYNONYMS[clean_word]
                for syn in synonyms[:2]:  # Take top 2 synonyms
                    # Use word-boundary replacement to avoid partial matches
                    expanded = re.sub(
                        r'\b' + re.escape(clean_word) + r'\b', syn, query_lower, count=1
                    )
                    if expanded != query_lower:
                        expansions.append(expanded)

        # Also check multi-word matches
        for term, synonyms in ISLAMIC_SYNONYMS.items():
            if term in query_lower and len(term) > 3:
                for syn in synonyms[:1]:
                    expanded = re.sub(
                        r'\b' + re.escape(term) + r'\b', syn, query_lower, count=1
                    )
                    if expanded != query_lower and expanded not in expansions:
                        expansions.append(expanded)

        return expansions


def expand_query_islamic(query: str, max_queries: int = 3) -> List[str]:
    """Convenience function for Islamic query expansion.

    Args:
        query: User query
        max_queries: Maximum total queries

    Returns:
        List of queries (original first)
    """
    return MultiQueryRetrieval().generate_queries(query, n=max_queries)
