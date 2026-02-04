"""
Islamic-Text-Aware Document Chunking

Specialized chunker for Islamic texts that respects the natural boundaries of:
- Quran verses (never splits mid-verse)
- Hadith narrations (preserves complete isnad + matn)
- Fiqh rulings (preserves chapter/section units)
- Tafseer commentary (keeps verse + commentary together)

This module integrates with the existing chunking framework as ChunkingMode.ISLAMIC.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .chunking import (
    BaseChunker,
    Chunk,
    ChunkingConfig,
    RecursiveChunker,
    HeadingChunker,
)
from .section_extractor import SectionExtractor, get_section_aware_citation


class IslamicSourceType(str, Enum):
    """Type of Islamic source text"""
    QURAN = "quran"
    HADITH = "hadith"
    TAFSEER = "tafseer"
    FIQH = "fiqh"
    GENERAL_ISLAMIC = "general_islamic"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Regex patterns for Islamic text boundary detection
# ---------------------------------------------------------------------------

# Quran reference patterns
QURAN_PATTERNS = [
    # English: Surah Al-Baqarah, Verse 255  /  (2:255)
    re.compile(r'\(\s*(\d{1,3})\s*:\s*(\d{1,3})\s*\)'),                            # (2:255)
    re.compile(r'(?:Surah|Chapter)\s+[\w\s-]+[,،:]\s*(?:Verse|Ayah|Ayat)\s*(\d+)', re.IGNORECASE),
    re.compile(r'(?:Quran|Qur\'?an|Q)\s*[\[\(]?\s*(\d{1,3})\s*:\s*(\d{1,3})', re.IGNORECASE),
    # Arabic: سورة البقرة آية ٢٥٥
    re.compile(r'سورة\s+[\u0600-\u06ff\s]+[،,]?\s*(?:آية|الآية)\s*[\d٠-٩]+'),
    re.compile(r'﴿[^﴾]+﴾'),  # Quranic brackets ﴿...﴾
    # Tanzil line format: 2|255|text
    re.compile(r'^\s*\d{1,3}\|\d{1,3}\|', re.MULTILINE),
]

# Tanzil format: sura|ayah|text (one verse per line)
TANZIL_LINE_PATTERN = re.compile(r'^\s*(\d{1,3})\|(\d{1,3})\|(.+)$')

# Hadith markers
HADITH_PATTERNS = [
    # Arabic isnad chain starters
    re.compile(r'(?:حدثنا|أخبرنا|عن|روى)\s+[\u0600-\u06ff\s]+(?:قال|أن)', re.UNICODE),
    # English narration markers
    re.compile(r'(?:Narrated|Reported\s+by|It\s+was\s+narrated\s+from)\s+[\w\s]+:', re.IGNORECASE),
    # Hadith collection references
    re.compile(
        r'(?:Sahih|Sunan|Musnad|Muwatta|Jami)\s*'
        r'(?:al-?)?'
        r'(?:Bukhari|Muslim|Abu\s*Dawud|Tirmidhi|Nasa.?i|Ibn\s*Majah|Ahmad|Malik)',
        re.IGNORECASE,
    ),
    # Numbered hadith marker
    re.compile(r'Hadith\s*(?:No\.?\s*)?#?\s*\d+', re.IGNORECASE),
]

# Fiqh section markers
FIQH_PATTERNS = [
    # Arabic chapter/section markers
    re.compile(r'^(?:باب|كتاب|فصل|مسألة|فرع)\s*[:：]?\s*.+', re.MULTILINE | re.UNICODE),
    # English school references
    re.compile(
        r'(?:According\s+to|The)\s+(?:Hanafi|Maliki|Shafi.?i|Hanbali)\s+(?:school|madhhab|madhab|view)',
        re.IGNORECASE,
    ),
    # Islamic jurisprudence structural markers
    re.compile(r'(?:Ruling|Fatwa|Verdict|Hukm)\s*[:：]', re.IGNORECASE),
]

# Tafseer patterns
TAFSEER_PATTERNS = [
    re.compile(r'(?:Tafsir|Tafseer|Commentary\s+on)\s+', re.IGNORECASE),
    re.compile(r'(?:Ibn\s+Kathir|al-?Tabari|al-?Qurtubi|al-?Razi|Jalalayn)', re.IGNORECASE),
    re.compile(r'(?:تفسير|شرح)\s+', re.UNICODE),
]

# Bismillah - marks new chapter/section
BISMILLAH_PATTERN = re.compile(r'بسم\s*الله\s*الرحمن\s*الرحيم')

# Arabic Unicode range for script density detection
ARABIC_RANGE = re.compile(r'[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]')


def detect_islamic_source_type(text: str) -> IslamicSourceType:
    """Detect the primary Islamic source type of a document.

    Uses a scoring approach: whichever type has the most pattern hits wins.
    """
    if not text:
        return IslamicSourceType.UNKNOWN

    sample = text[:20000]  # Sample first 20K chars for efficiency

    scores = {
        IslamicSourceType.QURAN: 0,
        IslamicSourceType.HADITH: 0,
        IslamicSourceType.TAFSEER: 0,
        IslamicSourceType.FIQH: 0,
    }

    for p in QURAN_PATTERNS:
        scores[IslamicSourceType.QURAN] += len(p.findall(sample))
    for p in HADITH_PATTERNS:
        scores[IslamicSourceType.HADITH] += len(p.findall(sample))
    for p in TAFSEER_PATTERNS:
        scores[IslamicSourceType.TAFSEER] += len(p.findall(sample))
    for p in FIQH_PATTERNS:
        scores[IslamicSourceType.FIQH] += len(p.findall(sample))

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    if scores[best] == 0:
        # Check if it's generally Islamic content (Arabic + Islamic keywords)
        arabic_chars = len(ARABIC_RANGE.findall(sample))
        total_chars = max(len(sample), 1)
        has_islamic_keywords = bool(re.search(
            r'(?:Islam|Muslim|Allah|Prophet|Muhammad|Quran|Hadith|Sunnah|'
            r'Fiqh|Shari|Ummah|Salah|Zakat|Hajj|Sawm|'
            r'الله|محمد|الإسلام|النبي|القرآن|الحديث|السنة)',
            sample, re.IGNORECASE
        ))
        if arabic_chars / total_chars > 0.1 or has_islamic_keywords:
            return IslamicSourceType.GENERAL_ISLAMIC
        return IslamicSourceType.UNKNOWN

    return best


def is_islamic_text(text: str) -> bool:
    """Quick heuristic check if text is Islamic content.

    Used by AutomaticChunker to decide whether to delegate to IslamicTextChunker.
    """
    if not text:
        return False

    sample = text[:10000]

    # Check Arabic script density
    arabic_chars = len(ARABIC_RANGE.findall(sample))
    total_chars = max(len(sample), 1)
    high_arabic = arabic_chars / total_chars > 0.15

    # Check for Islamic markers
    islamic_markers = 0
    for patterns in [QURAN_PATTERNS, HADITH_PATTERNS, FIQH_PATTERNS, TAFSEER_PATTERNS]:
        for p in patterns:
            if p.search(sample):
                islamic_markers += 1

    # Bismillah is a strong signal
    if BISMILLAH_PATTERN.search(sample):
        islamic_markers += 3

    return islamic_markers >= 2 or (high_arabic and islamic_markers >= 1)


class IslamicTextChunker(BaseChunker):
    """
    Islamic-text-aware chunker that recognizes Quran verses, Hadith narrations,
    Fiqh rulings, and Tafseer commentary as natural chunk boundaries.

    Strategy:
    1. Detect document source type (Quran, Hadith, Fiqh, Tafseer)
    2. Apply type-specific boundary detection
    3. Group content units into chunks respecting size limits
    4. Fallback to RecursiveChunker for unrecognized sections
    5. (Optional) Apply strict section traceability for Imam-type datasets
    """

    def __init__(self, config: ChunkingConfig):
        super().__init__(config)
        self.section_extractor = SectionExtractor() if config.strict_section_traceability else None

    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []

        source_type = detect_islamic_source_type(text)

        if source_type == IslamicSourceType.QURAN:
            chunks = self._chunk_quran(text)
        elif source_type == IslamicSourceType.HADITH:
            chunks = self._chunk_hadith(text)
        elif source_type == IslamicSourceType.FIQH:
            chunks = self._chunk_fiqh(text)
        elif source_type == IslamicSourceType.TAFSEER:
            chunks = self._chunk_tafseer(text)
        else:
            # General Islamic or unknown - use heading-aware chunker with Islamic patterns
            chunks = self._chunk_general_islamic(text)

        # Tag all chunks with source type
        for c in chunks:
            c.metadata["islamic_source_type"] = source_type.value

        # Apply strict section traceability if enabled
        if self.config.strict_section_traceability:
            chunks = self._apply_section_traceability(text, chunks)

        return chunks

    def _apply_section_traceability(self, text: str, chunks: List[Chunk]) -> List[Chunk]:
        """
        Apply strict section traceability to all chunks.
        
        Ensures every chunk has:
        - section_title: The section/chapter heading
        - citation_text: Formatted citation including section
        """
        if not self.section_extractor:
            return chunks

        # Get section metadata for all chunks
        section_metadata_list = self.section_extractor.assign_section_to_chunks(text, chunks)

        # Apply section metadata to chunks
        for i, (chunk, section_metadata) in enumerate(zip(chunks, section_metadata_list)):
            if not section_metadata.get('section_title'):
                # Force section title if missing
                forced = self.section_extractor.force_section_title(
                    [chunk],
                    document_title=chunk.metadata.get('source_document'),
                    default_section="Main Content"
                )
                if forced:
                    section_metadata.update(forced[0])

            # Update chunk metadata with section info
            chunk.metadata.update(section_metadata)

            # Build section-aware citation
            doc_name = chunk.metadata.get('source_document') or chunk.metadata.get('document_title')
            citation = get_section_aware_citation(
                chunk.metadata,
                document_name=doc_name,
                position=chunk.metadata.get('paragraph_index') or i
            )
            
            # Only set citation_text if not already present from Islamic metadata
            if not chunk.metadata.get('citation_text'):
                chunk.metadata['citation_text'] = citation
            elif section_metadata.get('section_title'):
                # Append section to existing citation
                existing = chunk.metadata['citation_text']
                section = section_metadata['section_title']
                if section not in existing:
                    chunk.metadata['citation_text'] = f"{existing} — Section: {section}"

        return chunks

    # ------------------------------------------------------------------
    # Quran chunking: group consecutive verses, never split mid-verse
    # ------------------------------------------------------------------

    def _chunk_quran(self, text: str) -> List[Chunk]:
        """Split Quran text preserving verse integrity.

        Splits at Surah boundaries (Bismillah) and groups consecutive verses
        to stay within chunk_size. Individual verses are never split.
        """
        from .chunking import get_token_counter
        
        # Prefer Tanzil line format if detected (sura|ayah|text)
        tanzil_verses, total_lines = self._parse_tanzil_lines(text)
        if tanzil_verses and total_lines > 0:
            ratio = len(tanzil_verses) / max(total_lines, 1)
            if ratio >= 0.7:
                return self._chunk_quran_tanzil_lines(tanzil_verses)

        # Split at Bismillah (new Surah marker)
        surah_sections = BISMILLAH_PATTERN.split(text)
        if len(surah_sections) <= 1:
            # No Bismillah found - try splitting by verse reference patterns
            return self._chunk_by_verse_groups(text)

        chunks: List[Chunk] = []
        
        # Use token-based limits when enabled
        if self.config.use_token_count:
            token_counter = get_token_counter()
            token_limit = self.config.token_limit
            
            for section in surah_sections:
                section = section.strip()
                if not section:
                    continue

                section_tokens = token_counter.count_tokens(section)
                if section_tokens <= token_limit:
                    chunks.append(self._create_chunk(
                        section, len(chunks),
                        {"islamic_source_type": "quran", "chunk_strategy": "surah_section"}
                    ))
                else:
                    # Section too large - split by verse groups within it
                    sub_chunks = self._chunk_by_verse_groups(section)
                    for sc in sub_chunks:
                        sc.index = len(chunks)
                        chunks.append(sc)
        else:
            # Character-based chunking (fallback)
            for section in surah_sections:
                section = section.strip()
                if not section:
                    continue

                if len(section) <= self.config.chunk_size:
                    chunks.append(self._create_chunk(
                        section, len(chunks),
                        {"islamic_source_type": "quran", "chunk_strategy": "surah_section"}
                    ))
                else:
                    # Section too large - split by verse groups within it
                    sub_chunks = self._chunk_by_verse_groups(section)
                    for sc in sub_chunks:
                        sc.index = len(chunks)
                        chunks.append(sc)

        return chunks or RecursiveChunker(self.config).chunk(text)

    def _parse_tanzil_lines(self, text: str) -> Tuple[List[Tuple[int, int, str]], int]:
        """Parse Tanzil line format into (sura, ayah, text)."""
        verses: List[Tuple[int, int, str]] = []
        lines = [line for line in text.splitlines() if line.strip()]
        for line in lines:
            match = TANZIL_LINE_PATTERN.match(line.strip())
            if not match:
                continue
            sura = int(match.group(1))
            ayah = int(match.group(2))
            verse_text = match.group(3).strip()
            if verse_text:
                verses.append((sura, ayah, verse_text))
        return verses, len(lines)

    def _chunk_quran_tanzil_lines(
        self, verses: List[Tuple[int, int, str]]
    ) -> List[Chunk]:
        """Create verse-grouped chunks from Tanzil line format.

        Preserves verse integrity while grouping consecutive verses to
        honor token/char limits (better chunk size stability).
        """
        from .chunking import get_token_counter

        chunks: List[Chunk] = []
        token_counter = get_token_counter() if self.config.use_token_count else None
        max_limit = self.config.token_limit if self.config.use_token_count else self.config.chunk_size

        current_sura: Optional[int] = None
        current_start_ayah: Optional[int] = None
        current_end_ayah: Optional[int] = None
        current_texts: List[str] = []
        current_size = 0

        def flush_group() -> None:
            nonlocal current_sura, current_start_ayah, current_end_ayah, current_texts, current_size
            if not current_texts or current_sura is None or current_start_ayah is None:
                return

            text = "\n".join(current_texts).strip()
            if not text:
                current_texts = []
                current_size = 0
                current_sura = None
                current_start_ayah = None
                current_end_ayah = None
                return

            ayah_start = current_start_ayah
            ayah_end = current_end_ayah if current_end_ayah is not None else ayah_start
            citation = (
                f"Quran {current_sura}:{ayah_start}"
                if ayah_end == ayah_start
                else f"Quran {current_sura}:{ayah_start}-{ayah_end}"
            )

            source_ref = {
                "sura": current_sura,
                "ayah_start": ayah_start,
                "ayah_end": ayah_end,
                "source": "tanzil",
                "text_version": "1.0",
                "download": "https://tanzil.net/pub/download/v1.0/",
            }
            if ayah_end == ayah_start:
                source_ref["ayah"] = ayah_start

            metadata = {
                "source_type": "quran",
                "source_reference": source_ref,
                "citation_text": citation,
                "section_header": f"Surah {current_sura}",
                "language": "ar",
            }

            chunk = self._create_chunk(text, len(chunks), metadata)
            chunks.append(chunk)
            current_texts = []
            current_size = 0
            current_sura = None
            current_start_ayah = None
            current_end_ayah = None

        for sura, ayah, verse_text in verses:
            verse_text = verse_text.strip()
            if not verse_text:
                continue

            # Reset group on sura change
            if current_sura is not None and sura != current_sura:
                flush_group()

            if current_sura is None:
                current_sura = sura
                current_start_ayah = ayah
                current_end_ayah = ayah

            verse_size = (
                token_counter.count_tokens(verse_text)
                if token_counter
                else len(verse_text)
            )

            # If single verse exceeds limit, emit it as a standalone chunk
            if max_limit and verse_size > max_limit:
                flush_group()
                current_sura = sura
                current_start_ayah = ayah
                current_end_ayah = ayah
                current_texts = [verse_text]
                current_size = verse_size
                flush_group()
                current_sura = None
                current_start_ayah = None
                current_end_ayah = None
                continue

            # If adding this verse exceeds limit, flush current group
            if current_texts and max_limit and (current_size + verse_size > max_limit):
                flush_group()
                current_sura = sura
                current_start_ayah = ayah
                current_end_ayah = ayah

            # Start new group if needed
            if current_sura is None:
                current_sura = sura
                current_start_ayah = ayah
                current_end_ayah = ayah

            current_texts.append(verse_text)
            current_end_ayah = ayah
            current_size += verse_size

        flush_group()
        return chunks

    def _chunk_by_verse_groups(self, text: str) -> List[Chunk]:
        """Group Quran verses into chunks respecting size limits.

        Verse boundaries detected by:
        - Verse number patterns: (2:255), ﴿...﴾
        - Arabic verse markers
        """
        # Try to split by verse reference markers
        # Pattern matches verse references like (2:255) or numbered verses
        verse_split = re.compile(
            r'(?=\(\s*\d{1,3}\s*:\s*\d{1,3}\s*\))'  # Look-ahead for (X:Y)
            r'|(?=﴿)'                                   # Look-ahead for Quranic brackets
        )

        parts = verse_split.split(text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            # Can't detect verse boundaries - fallback
            return RecursiveChunker(self.config).chunk(text)

        return self._merge_parts_into_chunks(
            parts,
            source_type="quran",
            strategy="verse_group",
        )

    # ------------------------------------------------------------------
    # Hadith chunking: preserve complete narrations (isnad + matn)
    # ------------------------------------------------------------------

    def _chunk_hadith(self, text: str) -> List[Chunk]:
        """Split Hadith text preserving complete narrations.

        Hadith boundaries detected by:
        - Isnad chain starters (حدثنا, أخبرنا, عن ... قال/أن)
        - English narration headers (Narrated by...)
        - Numbered hadith markers (Hadith No. X)
        - Collection references (Sahih Bukhari, Book X)
        """
        # Build a combined pattern for hadith boundaries
        boundary_patterns = [
            r'(?:حدثنا|أخبرنا)\s+',                          # Arabic isnad start
            r'(?:Narrated|Reported\s+by)\s+[\w\s]+:',         # English narration
            r'Hadith\s*(?:No\.?\s*)?#?\s*\d+',                # Numbered hadith
            r'(?:Book|Chapter|باب|كتاب)\s+(?:of\s+)?[\w\s]+', # Book/chapter boundary
        ]
        combined = '|'.join(f'(?:{p})' for p in boundary_patterns)
        hadith_boundary = re.compile(f'(?=(?:^|\\n)\\s*(?:{combined}))', re.MULTILINE | re.IGNORECASE)

        parts = hadith_boundary.split(text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            # Try chapter-based splitting
            return self._chunk_by_chapters(text, source_type="hadith")

        return self._merge_parts_into_chunks(
            parts,
            source_type="hadith",
            strategy="narration",
        )

    # ------------------------------------------------------------------
    # Fiqh chunking: preserve chapter/section/ruling units
    # ------------------------------------------------------------------

    def _chunk_fiqh(self, text: str) -> List[Chunk]:
        """Split Fiqh text by chapter/section boundaries.

        Preserves complete ruling units (masala/far') and chapter structure.
        """
        return self._chunk_by_chapters(text, source_type="fiqh")

    # ------------------------------------------------------------------
    # Tafseer chunking: keep verse + commentary together
    # ------------------------------------------------------------------

    def _chunk_tafseer(self, text: str) -> List[Chunk]:
        """Split Tafseer text keeping verse + commentary pairs together.

        Boundaries are verse commentary sections.
        """
        # Tafseer typically has verse references followed by commentary
        # Try to detect verse+commentary blocks
        verse_commentary = re.compile(
            r'(?=(?:^|\n)\s*'  # Start of line
            r'(?:'
            r'\(\s*\d{1,3}\s*:\s*\d{1,3}\s*\)'  # (2:255)
            r'|﴿'                                   # Quranic bracket
            r'|(?:Verse|Ayah|آية)\s*\d+'            # Verse N
            r'|(?:Surah|سورة)\s+'                    # Surah name
            r'))',
            re.MULTILINE | re.IGNORECASE
        )

        parts = verse_commentary.split(text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return self._chunk_by_chapters(text, source_type="tafseer")

        return self._merge_parts_into_chunks(
            parts,
            source_type="tafseer",
            strategy="verse_commentary",
        )

    # ------------------------------------------------------------------
    # General Islamic text: heading-aware with Islamic patterns
    # ------------------------------------------------------------------

    def _chunk_general_islamic(self, text: str) -> List[Chunk]:
        """Chunk general Islamic text using enhanced heading detection."""
        # Use HeadingChunker with Islamic heading patterns added
        config_copy = ChunkingConfig(
            mode=self.config.mode,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            max_chunk_size=self.config.max_chunk_size,
            min_chunk_size=self.config.min_chunk_size,
            separators=self.config.separators,
            heading_patterns=self.config.heading_patterns + [
                r'^(?:باب|كتاب|فصل|مسألة|فرع)\s+.+$',    # Arabic chapter markers
                r'^(?:Surah|Chapter)\s+\d+',                  # Quran chapter markers
                r'^(?:Book|Kitab)\s+\d+',                     # Hadith book markers
                r'^(?:Section|Part)\s+\d+',                   # Section markers
            ],
        )
        heading_count = 0
        combined = '|'.join(f'({p})' for p in config_copy.heading_patterns)
        for line in text.split('\n'):
            if re.match(combined, line.strip(), re.MULTILINE):
                heading_count += 1

        if heading_count >= 3:
            return HeadingChunker(config_copy).chunk(text)

        return RecursiveChunker(self.config).chunk(text)

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _chunk_by_chapters(self, text: str, source_type: str) -> List[Chunk]:
        """Split text by chapter/section boundaries (باب/كتاب/فصل or Book/Chapter)."""
        chapter_pattern = re.compile(
            r'(?=(?:^|\n)\s*(?:'
            r'باب\s+|كتاب\s+|فصل\s+|مسألة\s+|فرع\s+'  # Arabic
            r'|Book\s+\d+|Chapter\s+\d+|Section\s+\d+'    # English
            r'|#{1,4}\s+'                                     # Markdown headings
            r'))',
            re.MULTILINE | re.IGNORECASE
        )

        parts = chapter_pattern.split(text)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) <= 1:
            return RecursiveChunker(self.config).chunk(text)

        return self._merge_parts_into_chunks(
            parts,
            source_type=source_type,
            strategy="chapter",
        )

    def _merge_parts_into_chunks(
        self,
        parts: List[str],
        source_type: str,
        strategy: str,
    ) -> List[Chunk]:
        """Merge content parts into chunks that respect size limits.

        Small parts are merged with neighbors; large parts are sub-chunked.
        """
        from .chunking import get_token_counter
        
        chunks: List[Chunk] = []
        current_text = ""
        
        # Use token-based limits when enabled
        if self.config.use_token_count:
            token_counter = get_token_counter()
            token_limit = self.config.token_limit
            current_tokens = 0

            for part in parts:
                if not part:
                    continue

                part_tokens = token_counter.count_tokens(part)

                # If adding this part would exceed token limit, flush current buffer
                if current_text and current_tokens + part_tokens > token_limit:
                    if current_text.strip():
                        chunks.append(self._create_chunk(
                            current_text.strip(),
                            len(chunks),
                            {"islamic_source_type": source_type, "chunk_strategy": strategy},
                        ))
                    current_text = ""
                    current_tokens = 0

                # If single part exceeds token limit, sub-chunk it
                if part_tokens > token_limit:
                    # Flush any accumulated text first
                    if current_text.strip():
                        chunks.append(self._create_chunk(
                            current_text.strip(),
                            len(chunks),
                            {"islamic_source_type": source_type, "chunk_strategy": strategy},
                        ))
                        current_text = ""
                        current_tokens = 0

                    # Sub-chunk the oversized part with RecursiveChunker (which supports token_limit)
                    sub_chunks = RecursiveChunker(self.config).chunk(part)
                    for sc in sub_chunks:
                        sc.index = len(chunks)
                        sc.metadata["islamic_source_type"] = source_type
                        sc.metadata["chunk_strategy"] = f"{strategy}_sub"
                        chunks.append(sc)
                else:
                    current_text = f"{current_text}\n\n{part}" if current_text else part
                    current_tokens += part_tokens

            # Flush remaining
            if current_text.strip():
                chunks.append(self._create_chunk(
                    current_text.strip(),
                    len(chunks),
                    {"islamic_source_type": source_type, "chunk_strategy": strategy},
                ))
        else:
            # Character-based chunking (fallback)
            chunk_size = self.config.chunk_size

            for part in parts:
                if not part:
                    continue

                # If adding this part would exceed size, flush current buffer
                if current_text and len(current_text) + len(part) + 2 > chunk_size:
                    if current_text.strip():
                        chunks.append(self._create_chunk(
                            current_text.strip(),
                            len(chunks),
                            {"islamic_source_type": source_type, "chunk_strategy": strategy},
                        ))
                    current_text = ""

                # If single part exceeds size, sub-chunk it
                if len(part) > chunk_size:
                    # Flush any accumulated text first
                    if current_text.strip():
                        chunks.append(self._create_chunk(
                            current_text.strip(),
                            len(chunks),
                            {"islamic_source_type": source_type, "chunk_strategy": strategy},
                        ))
                        current_text = ""

                    # Sub-chunk the oversized part with RecursiveChunker
                    sub_chunks = RecursiveChunker(self.config).chunk(part)
                    for sc in sub_chunks:
                        sc.index = len(chunks)
                        sc.metadata["islamic_source_type"] = source_type
                        sc.metadata["chunk_strategy"] = f"{strategy}_sub"
                        chunks.append(sc)
                else:
                    current_text = f"{current_text}\n\n{part}" if current_text else part

            # Flush remaining
            if current_text.strip():
                chunks.append(self._create_chunk(
                    current_text.strip(),
                    len(chunks),
                    {"islamic_source_type": source_type, "chunk_strategy": strategy},
                ))

        return chunks or RecursiveChunker(self.config).chunk("\n\n".join(parts))
