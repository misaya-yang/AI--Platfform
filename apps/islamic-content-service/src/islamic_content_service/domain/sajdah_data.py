"""
Sajdah (prostration) points in the Qur'an.

There are 15 canonical verses where a prostration (sajdah at-tilawah) is either
obligatory or recommended. The classification varies by school of jurisprudence:

- **obligatory (wajib)**: most schools; required if recited or heard
- **recommended (mustahabb)**: some schools consider it only recommended

Upstream Quran Foundation does not expose this classification. Stored here as
canonical constants (same pattern as ``juz_data.py``).

Reference: classical fiqh manuals; the list matches Quran.com, Tanzil, and the
standard Uthmani mushaf sajdah markers.
"""

from __future__ import annotations

from typing import TypedDict


class SajdahPoint(TypedDict):
    verse_key: str
    surah_number: int
    ayah_number: int
    sajdah_type: str  # "obligatory" | "recommended"
    sajdah_number: int  # 1..15 in recitation order


SAJDAH_POINTS: list[SajdahPoint] = [
    {"verse_key": "7:206",  "surah_number": 7,  "ayah_number": 206, "sajdah_type": "recommended", "sajdah_number": 1},
    {"verse_key": "13:15",  "surah_number": 13, "ayah_number": 15,  "sajdah_type": "recommended", "sajdah_number": 2},
    {"verse_key": "16:50",  "surah_number": 16, "ayah_number": 50,  "sajdah_type": "recommended", "sajdah_number": 3},
    {"verse_key": "17:109", "surah_number": 17, "ayah_number": 109, "sajdah_type": "recommended", "sajdah_number": 4},
    {"verse_key": "19:58",  "surah_number": 19, "ayah_number": 58,  "sajdah_type": "recommended", "sajdah_number": 5},
    {"verse_key": "22:18",  "surah_number": 22, "ayah_number": 18,  "sajdah_type": "recommended", "sajdah_number": 6},
    {"verse_key": "22:77",  "surah_number": 22, "ayah_number": 77,  "sajdah_type": "recommended", "sajdah_number": 7},
    {"verse_key": "25:60",  "surah_number": 25, "ayah_number": 60,  "sajdah_type": "recommended", "sajdah_number": 8},
    {"verse_key": "27:26",  "surah_number": 27, "ayah_number": 26,  "sajdah_type": "recommended", "sajdah_number": 9},
    {"verse_key": "32:15",  "surah_number": 32, "ayah_number": 15,  "sajdah_type": "obligatory",  "sajdah_number": 10},
    {"verse_key": "38:24",  "surah_number": 38, "ayah_number": 24,  "sajdah_type": "recommended", "sajdah_number": 11},
    {"verse_key": "41:38",  "surah_number": 41, "ayah_number": 38,  "sajdah_type": "obligatory",  "sajdah_number": 12},
    {"verse_key": "53:62",  "surah_number": 53, "ayah_number": 62,  "sajdah_type": "obligatory",  "sajdah_number": 13},
    {"verse_key": "84:21",  "surah_number": 84, "ayah_number": 21,  "sajdah_type": "obligatory",  "sajdah_number": 14},
    {"verse_key": "96:19",  "surah_number": 96, "ayah_number": 19,  "sajdah_type": "obligatory",  "sajdah_number": 15},
]

TOTAL_SAJDAH_COUNT = len(SAJDAH_POINTS)
