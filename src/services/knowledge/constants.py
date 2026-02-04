"""
Knowledge service shared constants.

This module contains constants used across multiple knowledge service modules
to avoid circular imports.
"""

from __future__ import annotations

from typing import Dict, List


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
