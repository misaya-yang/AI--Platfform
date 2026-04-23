"""Citation authority ordering for KB-derived contexts.

Quran (1) > Hadith (2) > Tafseer (3) > Fiqh (4) > General (5) > Unknown (6).

String-keyed dict so service-agnostic callers don't need the
``IslamicSourceType`` enum from the KB service.
"""

from __future__ import annotations

AUTHORITY_ORDER: dict[str, int] = {
    "quran": 1,
    "hadith": 2,
    "tafseer": 3,
    "fiqh": 4,
    "general_islamic": 5,
    "unknown": 6,
}


def get_authority_order(source_type: str) -> int:
    """Return the authority rank (1 = highest) for a citation source type."""
    return AUTHORITY_ORDER.get(source_type, 6)


__all__ = ["AUTHORITY_ORDER", "get_authority_order"]
