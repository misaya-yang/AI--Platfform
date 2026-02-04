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
from typing import List

from .constants import ISLAMIC_SYNONYMS


# Re-export for backward compatibility
__all__ = ["MultiQueryRetrieval", "ISLAMIC_SYNONYMS", "expand_query_islamic"]


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
