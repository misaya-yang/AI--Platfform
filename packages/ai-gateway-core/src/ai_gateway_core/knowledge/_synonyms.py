"""Islamic-term synonym dictionary used by assistant quality policies."""

from __future__ import annotations

ISLAMIC_SYNONYMS: dict[str, list[str]] = {
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
}

__all__ = ["ISLAMIC_SYNONYMS"]
