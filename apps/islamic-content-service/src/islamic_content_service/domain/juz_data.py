"""
Traditional Juz (para) names.

Each of the 30 Juz is named after the opening words of its first verse
(the "incipit"). These names are not exposed by Quran Foundation's upstream
API — they are canonical and universal across the Muslim world, so we keep
them as service-side constants.
"""

from __future__ import annotations

from typing import TypedDict


class JuzName(TypedDict):
    name_arabic: str
    name_simple: str
    name_transliteration: str


JUZ_NAMES: dict[int, JuzName] = {
    1:  {"name_arabic": "الم",                    "name_simple": "Alif Lam Mim",           "name_transliteration": "Alif Lām Mīm"},
    2:  {"name_arabic": "سَيَقُولُ",              "name_simple": "Sayaqul",                 "name_transliteration": "Sayaqūlu"},
    3:  {"name_arabic": "تِلْكَ الرُّسُلُ",        "name_simple": "Tilkar Rusul",            "name_transliteration": "Tilka r-Rusul"},
    4:  {"name_arabic": "لَنْ تَنَالُوا",          "name_simple": "Lan Tanaalu",             "name_transliteration": "Lan Tanālū"},
    5:  {"name_arabic": "وَالْمُحْصَنَاتُ",        "name_simple": "Wal Mohsanat",            "name_transliteration": "Wa-l-Muḥṣanāt"},
    6:  {"name_arabic": "لَا يُحِبُّ اللَّهُ",     "name_simple": "La Yuhibbullah",          "name_transliteration": "Lā Yuḥibbu llāhu"},
    7:  {"name_arabic": "وَإِذَا سَمِعُوا",        "name_simple": "Wa Iza Samiu",            "name_transliteration": "Wa Iḏā Samiʿū"},
    8:  {"name_arabic": "وَلَوْ أَنَّنَا",         "name_simple": "Wa Lau Annana",           "name_transliteration": "Wa Law Annanā"},
    9:  {"name_arabic": "قَالَ الْمَلَأُ",         "name_simple": "Qalal Malau",             "name_transliteration": "Qāla l-Malaʾu"},
    10: {"name_arabic": "وَاعْلَمُوا",             "name_simple": "Wa A'lamu",               "name_transliteration": "Wa-ʿlamū"},
    11: {"name_arabic": "يَعْتَذِرُونَ",           "name_simple": "Yatazeroon",              "name_transliteration": "Yaʿtaḏirūna"},
    12: {"name_arabic": "وَمَا مِنْ دَابَّةٍ",      "name_simple": "Wa Mamin Dabbah",         "name_transliteration": "Wa Mā min Dābbatin"},
    13: {"name_arabic": "وَمَا أُبَرِّئُ",          "name_simple": "Wa Ma Ubarriu",           "name_transliteration": "Wa Mā Ubarri'u"},
    14: {"name_arabic": "رُبَمَا",                 "name_simple": "Rubama",                  "name_transliteration": "Rubamā"},
    15: {"name_arabic": "سُبْحَانَ الَّذِي",        "name_simple": "Subhanallazi",            "name_transliteration": "Subḥāna l-laḏī"},
    16: {"name_arabic": "قَالَ أَلَمْ",            "name_simple": "Qal Alam",                "name_transliteration": "Qāla Alam"},
    17: {"name_arabic": "اقْتَرَبَ",               "name_simple": "Iqtarabath",              "name_transliteration": "Iqtaraba"},
    18: {"name_arabic": "قَدْ أَفْلَحَ",           "name_simple": "Qad Aflaha",              "name_transliteration": "Qad Aflaḥa"},
    19: {"name_arabic": "وَقَالَ الَّذِينَ",        "name_simple": "Wa Qalallazina",          "name_transliteration": "Wa Qāla l-laḏīna"},
    20: {"name_arabic": "أَمَّنْ خَلَقَ",          "name_simple": "A'man Khalaq",            "name_transliteration": "Amman Khalaqa"},
    21: {"name_arabic": "اتْلُ مَا أُوحِيَ",       "name_simple": "Utlu Ma Oohiya",          "name_transliteration": "Utlu Mā Ūḥiya"},
    22: {"name_arabic": "وَمَنْ يَقْنُتْ",         "name_simple": "Wa Man Yaqnut",           "name_transliteration": "Wa-Man Yaqnut"},
    23: {"name_arabic": "وَمَا لِيَ",              "name_simple": "Wa Maliya",               "name_transliteration": "Wa Mā Liya"},
    24: {"name_arabic": "فَمَنْ أَظْلَمُ",         "name_simple": "Faman Azlam",             "name_transliteration": "Fa-Man Aẓlamu"},
    25: {"name_arabic": "إِلَيْهِ يُرَدُّ",         "name_simple": "Elahe Yuruddu",           "name_transliteration": "Ilayhi Yuraddu"},
    26: {"name_arabic": "حم",                     "name_simple": "Ha Meem",                 "name_transliteration": "Ḥā Mīm"},
    27: {"name_arabic": "قَالَ فَمَا خَطْبُكُمْ",   "name_simple": "Qala Fa Ma Khatbukum",    "name_transliteration": "Qāla Fa-Mā Khaṭbukum"},
    28: {"name_arabic": "قَدْ سَمِعَ اللَّهُ",     "name_simple": "Qad Sami Allah",          "name_transliteration": "Qad Samiʿa llāhu"},
    29: {"name_arabic": "تَبَارَكَ الَّذِي",        "name_simple": "Tabarakallazi",           "name_transliteration": "Tabāraka l-laḏī"},
    30: {"name_arabic": "عَمَّ",                  "name_simple": "Amma",                    "name_transliteration": "ʿAmma"},
}

TOTAL_JUZ_COUNT = 30
