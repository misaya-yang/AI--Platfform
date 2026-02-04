"""
Islamic Metadata Extractor

Extracts structured metadata from Islamic text chunks for:
- Source type detection (Quran, Hadith, Tafseer, Fiqh)
- Citation reference extraction (Surah:Verse, Book:Hadith, etc.)
- Pre-formatted citation text per Wahda AI-Imam requirements
- Language detection (Arabic, English, bilingual)
- Madhab (Islamic school of thought) detection

Citation formats follow the Wahda AI-Imam Configuration spec:
- Quran: "Quran [Chapter]:[Verse] - [Translation]"
- Hadith: "Sahih Bukhari, Book [X], Hadith [X]"
- Tafseer: "Tafsir Ibn Kathir, Surah [X], Verse [X]"
- Fiqh: "Islamic Jurisprudence According to the Four Sunni Schools, [School], [Topic]"
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .islamic_chunking import (
    IslamicSourceType,
    detect_islamic_source_type,
    QURAN_PATTERNS,
    HADITH_PATTERNS,
    FIQH_PATTERNS,
    TAFSEER_PATTERNS,
    ARABIC_RANGE,
)


# Surah name lookup (number -> English name)
SURAH_NAMES: Dict[int, str] = {
    1: "Al-Fatihah", 2: "Al-Baqarah", 3: "Aal-E-Imran", 4: "An-Nisa",
    5: "Al-Ma'idah", 6: "Al-An'am", 7: "Al-A'raf", 8: "Al-Anfal",
    9: "At-Tawbah", 10: "Yunus", 11: "Hud", 12: "Yusuf",
    13: "Ar-Ra'd", 14: "Ibrahim", 15: "Al-Hijr", 16: "An-Nahl",
    17: "Al-Isra", 18: "Al-Kahf", 19: "Maryam", 20: "Ta-Ha",
    21: "Al-Anbiya", 22: "Al-Hajj", 23: "Al-Mu'minun", 24: "An-Nur",
    25: "Al-Furqan", 26: "Ash-Shu'ara", 27: "An-Naml", 28: "Al-Qasas",
    29: "Al-Ankabut", 30: "Ar-Rum", 31: "Luqman", 32: "As-Sajdah",
    33: "Al-Ahzab", 34: "Saba", 35: "Fatir", 36: "Ya-Sin",
    37: "As-Saffat", 38: "Sad", 39: "Az-Zumar", 40: "Ghafir",
    41: "Fussilat", 42: "Ash-Shura", 43: "Az-Zukhruf", 44: "Ad-Dukhan",
    45: "Al-Jathiyah", 46: "Al-Ahqaf", 47: "Muhammad", 48: "Al-Fath",
    49: "Al-Hujurat", 50: "Qaf", 51: "Adh-Dhariyat", 52: "At-Tur",
    53: "An-Najm", 54: "Al-Qamar", 55: "Ar-Rahman", 56: "Al-Waqi'ah",
    57: "Al-Hadid", 58: "Al-Mujadilah", 59: "Al-Hashr", 60: "Al-Mumtahanah",
    61: "As-Saff", 62: "Al-Jumu'ah", 63: "Al-Munafiqun", 64: "At-Taghabun",
    65: "At-Talaq", 66: "At-Tahrim", 67: "Al-Mulk", 68: "Al-Qalam",
    69: "Al-Haqqah", 70: "Al-Ma'arij", 71: "Nuh", 72: "Al-Jinn",
    73: "Al-Muzzammil", 74: "Al-Muddaththir", 75: "Al-Qiyamah", 76: "Al-Insan",
    77: "Al-Mursalat", 78: "An-Naba", 79: "An-Nazi'at", 80: "Abasa",
    81: "At-Takwir", 82: "Al-Infitar", 83: "Al-Mutaffifin", 84: "Al-Inshiqaq",
    85: "Al-Buruj", 86: "At-Tariq", 87: "Al-A'la", 88: "Al-Ghashiyah",
    89: "Al-Fajr", 90: "Al-Balad", 91: "Ash-Shams", 92: "Al-Layl",
    93: "Ad-Duha", 94: "Ash-Sharh", 95: "At-Tin", 96: "Al-Alaq",
    97: "Al-Qadr", 98: "Al-Bayyinah", 99: "Az-Zalzalah", 100: "Al-Adiyat",
    101: "Al-Qari'ah", 102: "At-Takathur", 103: "Al-Asr", 104: "Al-Humazah",
    105: "Al-Fil", 106: "Quraysh", 107: "Al-Ma'un", 108: "Al-Kawthar",
    109: "Al-Kafirun", 110: "An-Nasr", 111: "Al-Masad", 112: "Al-Ikhlas",
    113: "Al-Falaq", 114: "An-Nas",
}

# Hadith collection name normalization
HADITH_COLLECTIONS: Dict[str, str] = {
    "bukhari": "Sahih Bukhari",
    "sahih bukhari": "Sahih Bukhari",
    "muslim": "Sahih Muslim",
    "sahih muslim": "Sahih Muslim",
    "abu dawud": "Sunan Abu Dawud",
    "tirmidhi": "Jami at-Tirmidhi",
    "nasa'i": "Sunan an-Nasa'i",
    "nasai": "Sunan an-Nasa'i",
    "ibn majah": "Sunan Ibn Majah",
    "ahmad": "Musnad Ahmad",
    "malik": "Muwatta Malik",
    "muwatta": "Muwatta Malik",
}

# Madhab detection patterns
MADHAB_PATTERNS = {
    "hanafi": re.compile(r'(?:Hanafi|حنفي|Abu\s*Hanifah|أبو\s*حنيفة)', re.IGNORECASE),
    "maliki": re.compile(r'(?:Maliki|مالكي|Imam\s*Malik|الإمام\s*مالك)', re.IGNORECASE),
    "shafii": re.compile(r'(?:Shafi.?i|شافعي|Imam\s*(?:al-?)?Shafi|الإمام\s*الشافعي)', re.IGNORECASE),
    "hanbali": re.compile(r'(?:Hanbali|حنبلي|Ahmad\s*(?:ibn|bin)\s*Hanbal|أحمد\s*بن\s*حنبل)', re.IGNORECASE),
}


class IslamicMetadataExtractor:
    """Extract structured Islamic metadata from chunk text and document context."""

    def extract(self, text: str, document_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extract all Islamic metadata from a text chunk.

        Returns dict with keys:
            source_type, source_reference, citation_text, language, madhab
        """
        doc_meta = document_metadata or {}
        result: Dict[str, Any] = {}

        # Source type detection
        source_type = self.detect_source_type(text)
        result["source_type"] = source_type.value

        # Source reference extraction
        source_ref = self._extract_reference(text, source_type)
        result["source_reference"] = source_ref

        # Pre-formatted citation
        result["citation_text"] = self.format_citation(source_type, source_ref, doc_meta)

        # Language detection
        result["language"] = self.detect_language(text)

        # Madhab detection
        madhab = self.detect_madhab(text)
        if madhab:
            result["madhab"] = madhab

        return result

    def detect_source_type(self, text: str) -> IslamicSourceType:
        """Detect the Islamic source type of a single chunk."""
        return detect_islamic_source_type(text)

    def detect_language(self, text: str) -> str:
        """Detect language: 'ar', 'en', or 'ar_en' (bilingual)."""
        if not text:
            return "en"

        sample = text[:5000]
        total = max(len(sample), 1)
        arabic_count = len(ARABIC_RANGE.findall(sample))
        arabic_ratio = arabic_count / total

        if arabic_ratio > 0.5:
            return "ar"
        elif arabic_ratio > 0.1:
            return "ar_en"
        return "en"

    def detect_madhab(self, text: str) -> Optional[str]:
        """Detect which madhab (school of thought) is referenced, if any."""
        if not text:
            return None

        detected = []
        for madhab, pattern in MADHAB_PATTERNS.items():
            if pattern.search(text):
                detected.append(madhab)

        if len(detected) == 1:
            return detected[0]
        elif len(detected) > 1:
            return "multiple"  # Multiple schools referenced
        return None

    def format_citation(
        self,
        source_type: IslamicSourceType,
        reference: Dict[str, Any],
        doc_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Format citation text per Wahda AI-Imam spec.

        Formats:
        - Quran: "Quran [Chapter]:[Verse] - [Surah Name]"
        - Hadith: "Sahih Bukhari, Book [X], Hadith [X]"
        - Tafseer: "Tafsir [Author], Surah [X], Verse [X]"
        - Fiqh: "[Book], [School], [Topic]"
        """
        def _doc_context(meta: Optional[Dict[str, Any]]) -> str:
            meta = meta or {}
            doc_name = meta.get("title") or meta.get("name") or meta.get("source_document") or ""
            section = meta.get("section_title") or ""
            para = meta.get("paragraph_index")
            if para is None:
                para = meta.get("chunk_index")
            if para is None:
                para = meta.get("position")
            parts = []
            if doc_name:
                parts.append(str(doc_name))
            if section:
                parts.append(f"Section {section}")
            if para is not None:
                parts.append(f"Paragraph {para}")
            return ", ".join(parts)

        doc_context = _doc_context(doc_meta)

        if not reference:
            # Fallback to document-level citation
            return doc_context or ""

        if source_type == IslamicSourceType.QURAN:
            surah = reference.get("surah")
            verse_start = reference.get("verse_start")
            surah_name = reference.get("surah_name", "")
            if surah and verse_start:
                verse_end = reference.get("verse_end")
                verse_str = str(verse_start)
                if verse_end and verse_end != verse_start:
                    verse_str = f"{verse_start}-{verse_end}"
                name_part = f" - {surah_name}" if surah_name else ""
                base = f"Quran {surah}:{verse_str}{name_part}"
                return f"{doc_context} — {base}" if doc_context else base
            return f"{doc_context} — Quran" if doc_context else "Quran"

        elif source_type == IslamicSourceType.HADITH:
            collection = reference.get("collection", "")
            book = reference.get("book")
            hadith_num = reference.get("hadith_number")
            narrator = reference.get("narrator", "")

            parts = []
            if collection:
                parts.append(collection)
            if book:
                parts.append(f"Book {book}")
            if hadith_num:
                parts.append(f"Hadith {hadith_num}")
            if narrator and not parts:
                parts.append(f"Narrated by {narrator}")

            base = ", ".join(parts) if parts else "Hadith"
            return f"{doc_context} — {base}" if doc_context else base

        elif source_type == IslamicSourceType.TAFSEER:
            author = reference.get("author", "")
            surah = reference.get("surah")
            verse = reference.get("verse")

            if author:
                base = f"Tafsir {author}"
            else:
                base = "Tafsir"

            if surah:
                try:
                    surah_name = SURAH_NAMES.get(int(surah), "")
                except (ValueError, TypeError):
                    surah_name = ""
                base += f", Surah {surah_name or surah}"
            if verse:
                base += f", Verse {verse}"
            return f"{doc_context} — {base}" if doc_context else base

        elif source_type == IslamicSourceType.FIQH:
            school = reference.get("school", "")
            topic = reference.get("topic", "")
            book = reference.get("book", "")

            parts = []
            if book:
                parts.append(book)
            elif school:
                parts.append(f"Islamic Jurisprudence According to the Four Sunni Schools")
            if school:
                parts.append(school.title())
            if topic:
                parts.append(topic)
            base = ", ".join(parts) if parts else "Islamic Jurisprudence"
            return f"{doc_context} — {base}" if doc_context else base

        return doc_context or ""

    # ------------------------------------------------------------------
    # Internal reference extraction
    # ------------------------------------------------------------------

    def _extract_reference(self, text: str, source_type: IslamicSourceType) -> Dict[str, Any]:
        """Extract structured reference data based on source type."""
        if source_type == IslamicSourceType.QURAN:
            return self._extract_quran_reference(text)
        elif source_type == IslamicSourceType.HADITH:
            return self._extract_hadith_reference(text)
        elif source_type == IslamicSourceType.TAFSEER:
            return self._extract_tafseer_reference(text)
        elif source_type == IslamicSourceType.FIQH:
            return self._extract_fiqh_reference(text)
        return {}

    def _extract_quran_reference(self, text: str) -> Dict[str, Any]:
        """Extract Quran reference: surah number, verse start/end, surah name."""
        ref: Dict[str, Any] = {}

        # Match (X:Y) pattern
        match = re.search(r'\(\s*(\d{1,3})\s*:\s*(\d{1,3})\s*\)', text)
        if match:
            surah = int(match.group(1))
            verse = int(match.group(2))
            ref["surah"] = surah
            ref["verse_start"] = verse
            ref["surah_name"] = SURAH_NAMES.get(surah, "")

            # Check for verse range: (2:255-257)
            range_match = re.search(
                r'\(\s*\d{1,3}\s*:\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\s*\)', text
            )
            if range_match:
                ref["verse_start"] = int(range_match.group(1))
                ref["verse_end"] = int(range_match.group(2))
            else:
                # Check for multiple verse refs to find range
                all_refs = re.findall(r'\(\s*' + str(surah) + r'\s*:\s*(\d{1,3})\s*\)', text)
                if len(all_refs) > 1:
                    verses = sorted(int(v) for v in all_refs)
                    ref["verse_start"] = verses[0]
                    ref["verse_end"] = verses[-1]

        # Try "Quran X:Y" format
        if not ref:
            match = re.search(r'(?:Quran|Qur\'?an|Q)\s*[\[\(]?\s*(\d{1,3})\s*:\s*(\d{1,3})', text, re.IGNORECASE)
            if match:
                surah = int(match.group(1))
                ref["surah"] = surah
                ref["verse_start"] = int(match.group(2))
                ref["surah_name"] = SURAH_NAMES.get(surah, "")

        # Try Surah name extraction
        if not ref.get("surah_name"):
            surah_match = re.search(
                r'(?:Surah|سورة)\s+([\w\s\'-]+?)(?:\s*[,،\(\[]|\s*$)',
                text, re.IGNORECASE
            )
            if surah_match:
                ref["surah_name"] = surah_match.group(1).strip()

        return ref

    def _extract_hadith_reference(self, text: str) -> Dict[str, Any]:
        """Extract Hadith reference: collection, book, hadith number, narrator."""
        ref: Dict[str, Any] = {}

        # Collection detection
        for key, canonical in HADITH_COLLECTIONS.items():
            if re.search(re.escape(key), text, re.IGNORECASE):
                ref["collection"] = canonical
                break

        # Book number
        book_match = re.search(r'Book\s+(?:No\.?\s*)?(\d+)', text, re.IGNORECASE)
        if book_match:
            ref["book"] = int(book_match.group(1))

        # Hadith number
        hadith_match = re.search(r'Hadith\s*(?:No\.?\s*)?#?\s*(\d+)', text, re.IGNORECASE)
        if hadith_match:
            ref["hadith_number"] = int(hadith_match.group(1))

        # Narrator
        narrator_match = re.search(
            r'(?:Narrated|Reported)\s+(?:by\s+)?([\w\s]+?)(?:\s*:|$)',
            text, re.IGNORECASE
        )
        if narrator_match:
            narrator = narrator_match.group(1).strip()
            # Clean up common suffixes
            narrator = re.sub(r'\s+(?:that|who|said)$', '', narrator, flags=re.IGNORECASE)
            if len(narrator) > 2:
                ref["narrator"] = narrator

        return ref

    def _extract_tafseer_reference(self, text: str) -> Dict[str, Any]:
        """Extract Tafseer reference: author, surah, verse."""
        ref: Dict[str, Any] = {}

        # Author detection
        authors = {
            "ibn kathir": "Ibn Kathir",
            "al-tabari": "at-Tabari",
            "tabari": "at-Tabari",
            "al-qurtubi": "al-Qurtubi",
            "qurtubi": "al-Qurtubi",
            "al-razi": "ar-Razi",
            "jalalayn": "al-Jalalayn",
            "ibn abbas": "Ibn Abbas",
            "al-sa'di": "as-Sa'di",
        }
        for key, canonical in authors.items():
            if re.search(re.escape(key), text, re.IGNORECASE):
                ref["author"] = canonical
                break

        # Surah + verse from Quran reference in tafseer context
        quran_ref = self._extract_quran_reference(text)
        if quran_ref.get("surah"):
            ref["surah"] = quran_ref["surah"]
            ref["surah_name"] = quran_ref.get("surah_name", "")
        if quran_ref.get("verse_start"):
            ref["verse"] = quran_ref["verse_start"]

        return ref

    def _extract_fiqh_reference(self, text: str) -> Dict[str, Any]:
        """Extract Fiqh reference: school, topic, book."""
        ref: Dict[str, Any] = {}

        # School detection
        for madhab, pattern in MADHAB_PATTERNS.items():
            if pattern.search(text):
                ref["school"] = madhab
                break

        # Topic detection from chapter headers
        topic_match = re.search(
            r'(?:باب|كتاب|فصل|مسألة|Chapter|Book|Section)\s*(?:of\s+)?(.+?)(?:\n|$)',
            text, re.IGNORECASE
        )
        if topic_match:
            topic = topic_match.group(1).strip()
            if len(topic) < 100:
                ref["topic"] = topic

        return ref


# Singleton-like authority ordering for citation sorting
AUTHORITY_ORDER = {
    IslamicSourceType.QURAN.value: 1,
    IslamicSourceType.HADITH.value: 2,
    IslamicSourceType.TAFSEER.value: 3,
    IslamicSourceType.FIQH.value: 4,
    IslamicSourceType.GENERAL_ISLAMIC.value: 5,
    IslamicSourceType.UNKNOWN.value: 6,
    "quran": 1,
    "hadith": 2,
    "tafseer": 3,
    "fiqh": 4,
    "general_islamic": 5,
    "unknown": 6,
}


def get_authority_order(source_type: str) -> int:
    """Get authority ordering for citation sorting.

    Quran (1) > Hadith (2) > Tafseer (3) > Fiqh (4) > Others (5+)
    """
    return AUTHORITY_ORDER.get(source_type, 6)
