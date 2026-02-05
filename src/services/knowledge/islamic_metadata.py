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
from typing import Any, Dict, List, Optional, Tuple

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
    "bulugh al-maram": "Bulugh Al-Maram",
    "bulugh al maram": "Bulugh Al-Maram",
    "bulugh almaram": "Bulugh Al-Maram",
    "bulugh": "Bulugh Al-Maram",
}

# Common Quran translation names used in document titles
QURAN_TRANSLATIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"saheeh\s+international", re.IGNORECASE), "Saheeh International"),
    (re.compile(r"sahih\s+international", re.IGNORECASE), "Saheeh International"),
    (re.compile(r"yusuf\s+ali", re.IGNORECASE), "Yusuf Ali"),
    (re.compile(r"pickthall", re.IGNORECASE), "Pickthall"),
    (re.compile(r"clearquran|the\s+clear\s+quran", re.IGNORECASE), "ClearQuran"),
    (re.compile(r"hilali[-\s]?khan", re.IGNORECASE), "Hilali-Khan"),
    (re.compile(r"muhammad\s+asad", re.IGNORECASE), "Muhammad Asad"),
]

# Known fiqh book naming patterns
FIQH_BOOK_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"islamic jurisprudence according to the four sunni schools", re.IGNORECASE),
     "Islamic Jurisprudence According to the Four Sunni Schools"),
    (re.compile(r"book\s+of\s+inheritance", re.IGNORECASE),
     "Islamic Jurisprudence According to the Four Sunni Schools"),
]

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
        doc_title = _get_doc_title(doc_meta)
        result: Dict[str, Any] = {}

        # Source type detection
        source_type = self.detect_source_type(text, doc_title=doc_title)
        result["source_type"] = source_type.value

        # Source reference extraction
        source_ref = self._extract_reference(text, source_type, doc_meta=doc_meta, doc_title=doc_title)
        result["source_reference"] = source_ref

        # Pre-formatted citation
        result["citation_text"] = self.format_citation(source_type, source_ref, doc_meta)

        # Language detection
        result["language"] = self.detect_language(text)

        # Madhab detection
        madhab = self.detect_madhab(text) or self._detect_madhab_from_title(doc_title)
        if madhab:
            result["madhab"] = madhab

        return result

    def detect_source_type(self, text: str, doc_title: Optional[str] = None) -> IslamicSourceType:
        """Detect the Islamic source type of a single chunk."""
        detected = detect_islamic_source_type(text)
        if detected not in (IslamicSourceType.UNKNOWN, IslamicSourceType.GENERAL_ISLAMIC):
            return detected
        hinted = _detect_source_type_from_doc_title(doc_title)
        return hinted or detected

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
        doc_meta = doc_meta or {}
        doc_context = _get_doc_title(doc_meta)

        if not reference:
            # Fallback to document-level citation
            return doc_context or ""

        if source_type == IslamicSourceType.QURAN:
            surah = reference.get("surah")
            verse_start = reference.get("verse_start")
            translation = reference.get("translation") or _detect_translation(doc_context or "")
            if surah and verse_start:
                verse_end = reference.get("verse_end")
                verse_str = str(verse_start)
                if verse_end and verse_end != verse_start:
                    verse_str = f"{verse_start}-{verse_end}"
                translation_part = f" - {translation}" if translation else ""
                return f"Quran {surah}:{verse_str}{translation_part}"
            return "Quran"

        elif source_type == IslamicSourceType.HADITH:
            collection = reference.get("collection", "")
            book = reference.get("book")
            hadith_num = reference.get("hadith_number")
            parts = []
            if collection:
                parts.append(collection)
            if book and collection != "Bulugh Al-Maram":
                parts.append(f"Book {book}")
            if hadith_num:
                parts.append(f"Hadith {hadith_num}")
            elif collection:
                parts.append("Hadith")
            if parts:
                return ", ".join(parts)
            return "Hadith"

        elif source_type == IslamicSourceType.TAFSEER:
            author = reference.get("author", "")
            surah = reference.get("surah")
            verse = reference.get("verse")

            if author:
                base = f"Tafsir {author}"
            else:
                base = "Tafsir"

            if surah:
                base += f", Surah {surah}"
            if verse:
                base += f", Verse {verse}"
            return base

        elif source_type == IslamicSourceType.FIQH:
            school = reference.get("school", "")
            topic = reference.get("topic", "")
            book = reference.get("book") or _detect_fiqh_book(doc_context or "")

            parts = []
            if book:
                parts.append(book)
            else:
                parts.append("Islamic Jurisprudence According to the Four Sunni Schools")
            if school:
                parts.append(school.title())
            if topic:
                parts.append(topic)
            return ", ".join(parts)

        return doc_context or ""

    # ------------------------------------------------------------------
    # Internal reference extraction
    # ------------------------------------------------------------------

    def _extract_reference(
        self,
        text: str,
        source_type: IslamicSourceType,
        doc_meta: Optional[Dict[str, Any]] = None,
        doc_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract structured reference data based on source type."""
        if source_type == IslamicSourceType.QURAN:
            return self._extract_quran_reference(text, doc_meta=doc_meta, doc_title=doc_title)
        elif source_type == IslamicSourceType.HADITH:
            return self._extract_hadith_reference(text, doc_meta=doc_meta, doc_title=doc_title)
        elif source_type == IslamicSourceType.TAFSEER:
            return self._extract_tafseer_reference(text)
        elif source_type == IslamicSourceType.FIQH:
            return self._extract_fiqh_reference(text, doc_meta=doc_meta, doc_title=doc_title)
        return {}

    def _extract_quran_reference(
        self,
        text: str,
        doc_meta: Optional[Dict[str, Any]] = None,
        doc_title: Optional[str] = None,
    ) -> Dict[str, Any]:
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

        # Try "X:Y" format at line start (common in translations)
        if not ref:
            match = re.search(r'^\s*(\d{1,3})\s*:\s*(\d{1,3})\b', text)
            if match:
                surah = int(match.group(1))
                ref["surah"] = surah
                ref["verse_start"] = int(match.group(2))
                ref["surah_name"] = SURAH_NAMES.get(surah, "")

        # Try Tanzil format "sura|ayah|text"
        if not ref:
            match = re.search(r'^\s*(\d{1,3})\s*\|\s*(\d{1,3})\s*\|', text)
            if match:
                surah = int(match.group(1))
                ref["surah"] = surah
                ref["verse_start"] = int(match.group(2))
                ref["surah_name"] = SURAH_NAMES.get(surah, "")

        # Fallback: explicit range "X:Y-Z" anywhere in text
        if not ref.get("surah"):
            range_match = re.search(r'\b(\d{1,3})\s*:\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\b', text)
            if range_match:
                surah = int(range_match.group(1))
                ref["surah"] = surah
                ref["verse_start"] = int(range_match.group(2))
                ref["verse_end"] = int(range_match.group(3))
                ref["surah_name"] = SURAH_NAMES.get(surah, "")

        # Fallback: any "X:Y" reference anywhere in text
        if not ref.get("surah"):
            all_refs = re.findall(r'\b(\d{1,3})\s*:\s*(\d{1,3})\b', text)
            if all_refs:
                surah = int(all_refs[0][0])
                ref["surah"] = surah
                ref["surah_name"] = SURAH_NAMES.get(surah, "")
                ref["verse_start"] = int(all_refs[0][1])

        # Try Surah name extraction
        if not ref.get("surah_name"):
            surah_match = re.search(
                r'(?:Surah|سورة)\s+([\w\s\'-]+?)(?:\s*[,،\(\[]|\s*$)',
                text, re.IGNORECASE
            )
            if surah_match:
                ref["surah_name"] = surah_match.group(1).strip()

        # Fallback: detect surah header + verse list like "Sūrah 107: al-Mā’ūn" and "1. ..."
        if not ref.get("surah"):
            header_match = re.search(r'(?:Surah|Sūrah|Chapter)\s+(\d{1,3})', text, re.IGNORECASE)
            if header_match:
                surah = int(header_match.group(1))
                ref["surah"] = surah
                ref["surah_name"] = ref.get("surah_name") or SURAH_NAMES.get(surah, "")
                verse_nums = re.findall(r'^\s*(\d{1,3})\.\s', text, re.MULTILINE)
                if verse_nums:
                    ref["verse_start"] = int(verse_nums[0])
                    ref["verse_end"] = int(verse_nums[-1])

        translation = _detect_translation(doc_title or _get_doc_title(doc_meta or {}))
        if translation:
            ref["translation"] = translation

        return ref

    def _extract_hadith_reference(
        self,
        text: str,
        doc_meta: Optional[Dict[str, Any]] = None,
        doc_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract Hadith reference: collection, book, hadith number, narrator."""
        ref: Dict[str, Any] = {}

        # Collection detection (prefer document title to avoid mis-attribution)
        ref["collection"] = _detect_hadith_collection_from_title(doc_title or _get_doc_title(doc_meta or {}))
        if not ref.get("collection"):
            for key, canonical in HADITH_COLLECTIONS.items():
                if re.search(re.escape(key), text, re.IGNORECASE):
                    ref["collection"] = canonical
                    break

        # Book number
        book_match = re.search(r'Book\s+(?:No\.?\s*)?(\d+)', text, re.IGNORECASE)
        if book_match:
            ref["book"] = int(book_match.group(1))

        # Hadith number
        sample = text[:800]
        hadith_match = re.search(r'Hadith\s*(?:No\.?\s*)?#?\s*(\d+)', sample, re.IGNORECASE)
        if hadith_match:
            ref["hadith_number"] = int(hadith_match.group(1))
        if "hadith_number" not in ref:
            para_match = re.search(r'Paragraph\s*(\d+)', sample, re.IGNORECASE)
            if para_match:
                ref["hadith_number"] = int(para_match.group(1))
        if "hadith_number" not in ref:
            lead_match = re.search(r'^\s*(\d{1,5})\s*(?:[.)-])\s*(?:Narrated|Reported|$)', sample, re.IGNORECASE | re.MULTILINE)
            if lead_match:
                ref["hadith_number"] = int(lead_match.group(1))
        if "hadith_number" not in ref:
            line_match = re.search(r'^\s*(\d{1,5})\s*(?:[.)-])\s+', sample, re.MULTILINE)
            if line_match:
                ref["hadith_number"] = int(line_match.group(1))
        if "hadith_number" not in ref:
            context_match = re.search(
                r'\b(?:bukhari|muslim|tirmidhi|nasai|ibn\s+majah|bulugh)\D{0,12}(\d{1,5})\b',
                sample,
                re.IGNORECASE,
            )
            if context_match:
                ref["hadith_number"] = int(context_match.group(1))

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

        # Bulugh Al-Maram uses hadith numbering; avoid "Book" in citations
        if ref.get("collection") == "Bulugh Al-Maram":
            if "hadith_number" not in ref and ref.get("book"):
                ref["hadith_number"] = ref.get("book")
            ref.pop("book", None)

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

    def _extract_fiqh_reference(
        self,
        text: str,
        doc_meta: Optional[Dict[str, Any]] = None,
        doc_title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract Fiqh reference: school, topic, book."""
        ref: Dict[str, Any] = {}

        # School detection
        for madhab, pattern in MADHAB_PATTERNS.items():
            if pattern.search(text):
                ref["school"] = madhab
                break
        if not ref.get("school"):
            ref["school"] = self._detect_madhab_from_title(doc_title or _get_doc_title(doc_meta or {}))

        # Topic detection from chapter headers
        topic_match = re.search(
            r'(?:باب|كتاب|فصل|مسألة|Chapter|Book|Section)\s*(?:of\s+)?(.+?)(?:\n|$)',
            text, re.IGNORECASE
        )
        if topic_match:
            topic = topic_match.group(1).strip()
            if len(topic) < 100:
                ref["topic"] = _normalize_topic(topic)

        # Fallback to section title or document title for topic
        if not ref.get("topic"):
            section_title = (doc_meta or {}).get("section_title")
            if section_title:
                ref["topic"] = _normalize_topic(section_title)
        if not ref.get("topic"):
            inferred_topic = _infer_topic_from_title(doc_title or _get_doc_title(doc_meta or {}))
            if inferred_topic:
                ref["topic"] = inferred_topic

        # Book detection
        if doc_title:
            ref["book"] = _detect_fiqh_book(doc_title)
        if not ref.get("book") and ref.get("school") and ref.get("topic"):
            ref["book"] = "Islamic Jurisprudence According to the Four Sunni Schools"

        return ref

    def _detect_madhab_from_title(self, doc_title: Optional[str]) -> Optional[str]:
        if not doc_title:
            return None
        for madhab, pattern in MADHAB_PATTERNS.items():
            if pattern.search(doc_title):
                return madhab
        return None


def _get_doc_title(meta: Dict[str, Any]) -> str:
    for key in ("title", "name", "source_document", "document_title"):
        value = meta.get(key)
        if value:
            return str(value)
    return ""


def _detect_translation(doc_title: str) -> Optional[str]:
    if not doc_title:
        return None
    for pattern, label in QURAN_TRANSLATIONS:
        if pattern.search(doc_title):
            return label
    return None


def _detect_source_type_from_doc_title(doc_title: Optional[str]) -> Optional[IslamicSourceType]:
    if not doc_title:
        return None
    title = doc_title.lower()
    if any(t in title for t in ("quran", "qur'an", "qurān", "القرآن", "القران")):
        return IslamicSourceType.QURAN
    if any(t in title for t in ("hadith", "sahih", "sunan", "bulugh", "bukhari", "muslim")):
        return IslamicSourceType.HADITH
    if any(t in title for t in ("tafsir", "tafseer", "تفسير")):
        return IslamicSourceType.TAFSEER
    if any(t in title for t in ("fiqh", "jurisprudence", "فقه", "sharia")):
        return IslamicSourceType.FIQH
    # Fiqh heuristics: madhab in title implies fiqh (if not already matched above)
    for _, pattern in MADHAB_PATTERNS.items():
        if pattern.search(doc_title):
            return IslamicSourceType.FIQH
    if re.search(r"book\s+of\s+\w+", title, re.IGNORECASE):
        return IslamicSourceType.FIQH
    return None


def _detect_hadith_collection_from_title(doc_title: str) -> Optional[str]:
    if not doc_title:
        return None
    lowered = doc_title.lower()
    for key, canonical in HADITH_COLLECTIONS.items():
        if key in lowered:
            return canonical
    return None


def _detect_fiqh_book(doc_title: str) -> Optional[str]:
    if not doc_title:
        return None
    for pattern, label in FIQH_BOOK_PATTERNS:
        if pattern.search(doc_title):
            return label
    return None


def _normalize_topic(topic: str) -> str:
    cleaned = topic.strip()
    cleaned = re.sub(r"^(?:the\s+)?(?:chapter|book|section)\s+of\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:the\s+)?(?:chapter|book|section)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\((?:hanafi|maliki|shafi.?i|hanbali)[^)]*\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.(?:pdf|docx)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"['\"`]+", "", cleaned)
    cleaned = re.sub(r"\bpage\s*\d+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[-–—]\s*\d+$", "", cleaned)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    cleaned = re.sub(r"[\s\.,;:]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _infer_topic_from_title(doc_title: str) -> Optional[str]:
    if not doc_title:
        return None
    # Try to extract topic from "(Hanafi)" style titles
    match = re.match(r"(.+?)\s*\((hanafi|maliki|shafi.?i|hanbali)\)", doc_title, re.IGNORECASE)
    if match:
        return _normalize_topic(match.group(1))
    return None


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
