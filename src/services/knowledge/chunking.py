"""
Production-grade Document Chunking Module

Supports multiple chunking strategies similar to Alibaba Cloud / Dify:
1. Automatic (智能切分) - Auto-detect best strategy
2. Fixed Size (按长度切分) - Fixed character/token count with overlap
3. Paragraph (按段落切分) - Split by paragraphs
4. Page (按页切分) - For PDF page-based splitting
5. Heading/Section (按标题切分) - Split by markdown/document headings
6. Regex (按正则切分) - Custom regex pattern
7. Separator (按符号切分) - Split by custom separators
8. Recursive (递归切分) - Hierarchical recursive splitting
9. Hierarchical/Parent-Child (父子切分) - Parent chunks with child sub-chunks

Additional features:
- Metadata extraction
- Text preprocessing
- Token counting
- Overlap handling
"""

from __future__ import annotations

import re
import json
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Callable


class ChunkingMode(str, Enum):
    """Supported chunking modes"""
    AUTOMATIC = "automatic"      # 智能切分
    FIXED_SIZE = "fixed_size"   # 按长度切分
    PARAGRAPH = "paragraph"      # 按段落切分
    PAGE = "page"               # 按页切分
    HEADING = "heading"         # 按标题切分
    REGEX = "regex"             # 按正则切分
    SEPARATOR = "separator"     # 按符号切分
    RECURSIVE = "recursive"     # 递归切分
    HIERARCHICAL = "hierarchical"  # 父子切分
    QA = "qa"                   # QA对切分


class ContentType(str, Enum):
    """Content type for segments (multimodal support)"""
    TEXT = "text"
    IMAGE = "image"
    MIXED = "mixed"  # Text chunk with associated images


@dataclass
class AssociatedImage:
    """
    Image associated with a text chunk (Dify-style Smart Attachment Handling).

    In multimodal RAG, text chunks can have up to 10 associated images.
    Images are associated based on proximity in the source document.

    Attributes:
        image_segment_id: ID of the image segment in the database
        storage_url: URL to the image in storage (S3/OSS)
        filename: Original filename of the image
        vlm_description: VLM-generated description of the image
        proximity_score: How closely related the image is to the text [0,1]
            - 1.0 = directly embedded inline
            - 0.7 = adjacent paragraph
            - 0.5 = same page/section
            - 0.3 = related but distant
        char_offset: Character offset in source document where image was found
        page_number: Page number in PDF/multi-page documents
    """
    image_segment_id: str
    storage_url: str
    filename: str = ""
    vlm_description: Optional[str] = None
    proximity_score: float = 1.0
    char_offset: int = 0
    page_number: Optional[int] = None
    media_type: str = "image/png"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "image_segment_id": self.image_segment_id,
            "storage_url": self.storage_url,
            "filename": self.filename,
            "vlm_description": self.vlm_description,
            "proximity_score": self.proximity_score,
            "char_offset": self.char_offset,
            "page_number": self.page_number,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AssociatedImage":
        """Create from dictionary"""
        return cls(
            image_segment_id=data.get("image_segment_id", ""),
            storage_url=data.get("storage_url", ""),
            filename=data.get("filename", ""),
            vlm_description=data.get("vlm_description"),
            proximity_score=float(data.get("proximity_score", 1.0)),
            char_offset=int(data.get("char_offset", 0)),
            page_number=data.get("page_number"),
            media_type=data.get("media_type", "image/png"),
        )


@dataclass
class ChunkingConfig:
    """Comprehensive chunking configuration

    Best Practices (2025):
    - Optimal chunk size: 400-512 tokens with 10-20% overlap
    - RecursiveCharacterTextSplitter achieves 85-90% recall
    - Parent-child chunking: parent 1500-2000 tokens, child 400-500 tokens
    - Semantic chunking offers 70% accuracy improvement for knowledge bases
    """
    mode: ChunkingMode = ChunkingMode.AUTOMATIC

    # Size parameters in CHARACTERS (optimized defaults based on industry research)
    # Note: 1 token ≈ 4-5 chars (English) or 1.5-2 chars (Chinese)
    # Target: 400-500 tokens = ~2000 chars for English
    chunk_size: int = 2000          # ~400-500 tokens for most RAG
    chunk_overlap: int = 300        # 15% overlap (~60-75 tokens)
    max_chunk_size: int = 3000      # Absolute max (~600-750 tokens)
    min_chunk_size: int = 400       # Min chunk size (~80-100 tokens)

    # Token-based (recommended for production)
    use_token_count: bool = True    # Token-based more accurate than char-based
    token_limit: int = 500          # ~500 tokens optimal for most embedding models

    # Separators (priority order: preserve semantic boundaries)
    separators: List[str] = field(default_factory=lambda: [
        "\n\n\n",    # Section breaks
        "\n\n",      # Paragraphs
        "\n",        # Lines
        "。",        # Chinese sentence end
        ".",         # English sentence end
        "！", "!",   # Exclamations
        "？", "?",   # Questions
        "；", ";",   # Semicolons
        "，", ",",   # Commas
        " ",         # Words (last resort)
    ])
    primary_separator: str = "\n\n"

    # Regex pattern (for regex mode)
    regex_pattern: str = ""

    # Heading detection (for heading mode)
    heading_patterns: List[str] = field(default_factory=lambda: [
        r"^#{1,6}\s+.+$",           # Markdown headings
        r"^第[一二三四五六七八九十\d]+[章节条款]",  # Chinese chapter markers
        r"^\d+\.\s+.+$",           # Numbered sections
        r"^[A-Z][A-Z\s]+:?\s*$",   # ALL CAPS headings
    ])

    # Hierarchical/Parent-child (optimized for retrieval)
    # Parent provides context, child provides precision
    # Note: sizes in CHARACTERS (1 token ≈ 4-5 chars English)
    parent_chunk_size: int = 8000   # ~1500-2000 tokens for context
    parent_overlap: int = 400       # 5% overlap between parents (~80-100 tokens)
    child_chunk_size: int = 2000    # ~400-500 tokens for precision
    child_overlap: int = 300        # 15% overlap between children (~60-75 tokens)
    parent_mode: str = "recursive"  # recursive | paragraph | section | full_doc

    # Image-aware chunking
    preserve_images: bool = True    # Keep images with surrounding context
    image_context_chars: int = 1000 # Characters around image to preserve (~200 tokens)
    
    # Preprocessing
    remove_extra_spaces: bool = True
    remove_urls_emails: bool = False
    normalize_whitespace: bool = True
    strip_html: bool = False
    
    # Metadata extraction
    extract_metadata: bool = False
    metadata_fields: List[str] = field(default_factory=lambda: ["title", "author", "date", "keywords"])
    
    # Page markers (for page mode)
    page_marker: str = r"\f"  # Form feed or custom marker
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChunkingConfig":
        if not data:
            return cls()
        
        mode_str = str(data.get("mode", "automatic")).lower()
        mode_map = {
            "automatic": ChunkingMode.AUTOMATIC,
            "auto": ChunkingMode.AUTOMATIC,
            "fixed_size": ChunkingMode.FIXED_SIZE,
            "fixed": ChunkingMode.FIXED_SIZE,
            "custom": ChunkingMode.FIXED_SIZE,
            "paragraph": ChunkingMode.PARAGRAPH,
            "page": ChunkingMode.PAGE,
            "heading": ChunkingMode.HEADING,
            "section": ChunkingMode.HEADING,
            "regex": ChunkingMode.REGEX,
            "separator": ChunkingMode.SEPARATOR,
            "recursive": ChunkingMode.RECURSIVE,
            "hierarchical": ChunkingMode.HIERARCHICAL,
            "parent_child": ChunkingMode.HIERARCHICAL,
            "qa": ChunkingMode.QA,
        }
        mode = mode_map.get(mode_str, ChunkingMode.AUTOMATIC)
        
        return cls(
            mode=mode,
            chunk_size=int(data.get("chunk_size")) if data.get("chunk_size") is not None else 2000,
            chunk_overlap=int(data.get("chunk_overlap")) if data.get("chunk_overlap") is not None else 300,
            max_chunk_size=int(data.get("max_chunk_size", 3000)),
            min_chunk_size=int(data.get("min_chunk_size", 400)),
            use_token_count=bool(data.get("use_token_count", True)),
            token_limit=int(data.get("token_limit", 500)),
            separators=data.get("separators") or ["\n\n\n", "\n\n", "\n", "。", ".", "！", "!", "？", "?", " "],
            primary_separator=str(data.get("primary_separator") or data.get("separator") or "\n\n"),
            regex_pattern=str(data.get("regex_pattern") or data.get("regex") or ""),
            heading_patterns=data.get("heading_patterns") or [],
            parent_chunk_size=int(data.get("parent_chunk_size", 8000)),
            parent_overlap=int(data.get("parent_overlap", 400)),
            child_chunk_size=int(data.get("child_chunk_size", 2000)),
            child_overlap=int(data.get("child_overlap", 300)),
            parent_mode=str(data.get("parent_mode", "recursive")),
            preserve_images=bool(data.get("preserve_images", True)),
            image_context_chars=int(data.get("image_context_chars", 1000)),
            remove_extra_spaces=bool(data.get("remove_extra_spaces", True)),
            remove_urls_emails=bool(data.get("remove_urls_emails", False)),
            normalize_whitespace=bool(data.get("normalize_whitespace", True)),
            strip_html=bool(data.get("strip_html", False)),
            extract_metadata=bool(data.get("extract_metadata", False)),
            metadata_fields=data.get("metadata_fields") or ["title", "author", "date"],
            page_marker=str(data.get("page_marker") or r"\f"),
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode.value,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "max_chunk_size": self.max_chunk_size,
            "min_chunk_size": self.min_chunk_size,
            "use_token_count": self.use_token_count,
            "token_limit": self.token_limit,
            "separators": self.separators,
            "primary_separator": self.primary_separator,
            "regex_pattern": self.regex_pattern,
            "heading_patterns": self.heading_patterns,
            "parent_chunk_size": self.parent_chunk_size,
            "parent_overlap": self.parent_overlap,
            "child_chunk_size": self.child_chunk_size,
            "child_overlap": self.child_overlap,
            "parent_mode": self.parent_mode,
            "preserve_images": self.preserve_images,
            "image_context_chars": self.image_context_chars,
            "remove_extra_spaces": self.remove_extra_spaces,
            "remove_urls_emails": self.remove_urls_emails,
            "normalize_whitespace": self.normalize_whitespace,
            "strip_html": self.strip_html,
            "extract_metadata": self.extract_metadata,
            "metadata_fields": self.metadata_fields,
            "page_marker": self.page_marker,
        }


@dataclass
class Chunk:
    """
    Represents a single text chunk with multimodal support.

    Multimodal RAG Extension (P3):
    - Chunks can have associated images (up to 10 per Dify pattern)
    - content_type indicates if this is a text, image, or mixed chunk
    - associated_images holds references to related image segments

    For image segments:
    - content_type = "image"
    - image_url, image_filename etc. are populated
    - vlm_description contains the VLM-generated description
    """
    text: str
    index: int = 0
    token_count: int = 0
    word_count: int = 0
    char_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    children: List["Chunk"] = field(default_factory=list)
    hash_id: str = ""

    # Multimodal fields (P3 extension)
    content_type: ContentType = ContentType.TEXT
    associated_images: List[AssociatedImage] = field(default_factory=list)

    # Image-specific fields (for image segments)
    image_url: Optional[str] = None
    image_filename: Optional[str] = None
    image_media_type: Optional[str] = None
    vlm_description: Optional[str] = None

    # Maximum images per chunk (Dify pattern)
    MAX_ASSOCIATED_IMAGES: int = field(default=10, repr=False)

    def __post_init__(self):
        if not self.hash_id and self.text:
            self.hash_id = hashlib.md5(self.text.encode()).hexdigest()[:12]
        if not self.char_count:
            self.char_count = len(self.text)
        if not self.word_count:
            self.word_count = len(self.text.split())
        if not self.token_count:
            # Rough estimate: ~4 chars per token for English, ~2 for Chinese
            self.token_count = self._estimate_tokens(self.text)

    @property
    def has_images(self) -> bool:
        """Check if this chunk has associated images"""
        return len(self.associated_images) > 0

    @property
    def image_count(self) -> int:
        """Number of associated images"""
        return len(self.associated_images)

    @property
    def is_image_segment(self) -> bool:
        """Check if this is an image segment (not text)"""
        return self.content_type == ContentType.IMAGE

    def add_associated_image(self, image: AssociatedImage) -> bool:
        """
        Add an associated image to this chunk.

        Args:
            image: The AssociatedImage to add

        Returns:
            True if added, False if max limit reached
        """
        if len(self.associated_images) >= self.MAX_ASSOCIATED_IMAGES:
            return False
        self.associated_images.append(image)
        # Update content type to mixed if this was pure text
        if self.content_type == ContentType.TEXT:
            self.content_type = ContentType.MIXED
        return True

    def get_images_sorted_by_proximity(self) -> List[AssociatedImage]:
        """Get associated images sorted by proximity score (highest first)"""
        return sorted(
            self.associated_images,
            key=lambda x: x.proximity_score,
            reverse=True
        )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count (rough approximation)"""
        if not text:
            return 0
        # Count CJK characters
        cjk_count = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
        # Non-CJK words
        non_cjk = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf]', '', text)
        word_count = len(non_cjk.split())
        # CJK: ~1.5 tokens per char, English: ~1 token per word
        return int(cjk_count * 0.7 + word_count * 1.3)

    def to_multimodal_dict(self) -> Dict[str, Any]:
        """Convert chunk to dictionary including multimodal fields"""
        return {
            "text": self.text,
            "index": self.index,
            "token_count": self.token_count,
            "char_count": self.char_count,
            "hash_id": self.hash_id,
            "content_type": self.content_type.value,
            "has_images": self.has_images,
            "image_count": self.image_count,
            "associated_images": [img.to_dict() for img in self.associated_images],
            "image_url": self.image_url,
            "image_filename": self.image_filename,
            "vlm_description": self.vlm_description,
            "metadata": self.metadata,
        }


class TextPreprocessor:
    """Text preprocessing utilities"""
    
    # Common patterns
    URL_PATTERN = re.compile(r'https?://\S+|www\.\S+')
    EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
    HTML_TAG_PATTERN = re.compile(r'<[^>]+>')
    EXTRA_SPACES_PATTERN = re.compile(r'[ \t]+')
    EXTRA_NEWLINES_PATTERN = re.compile(r'\n{3,}')
    
    @classmethod
    def preprocess(cls, text: str, config: ChunkingConfig) -> str:
        """Apply preprocessing based on config"""
        if not text:
            return ""
        
        result = text
        
        if config.strip_html:
            result = cls.HTML_TAG_PATTERN.sub(' ', result)
        
        if config.remove_urls_emails:
            result = cls.URL_PATTERN.sub(' ', result)
            result = cls.EMAIL_PATTERN.sub(' ', result)
        
        if config.normalize_whitespace:
            result = result.replace('\r\n', '\n').replace('\r', '\n')
            result = cls.EXTRA_NEWLINES_PATTERN.sub('\n\n', result)
        
        if config.remove_extra_spaces:
            result = cls.EXTRA_SPACES_PATTERN.sub(' ', result)
        
        return result.strip()
    
    @classmethod
    def extract_metadata(cls, text: str, fields: List[str]) -> Dict[str, Any]:
        """Extract metadata from document text"""
        metadata = {}
        
        # Try to extract title (first line or heading)
        if "title" in fields:
            lines = text.strip().split('\n')
            if lines:
                first_line = lines[0].strip()
                if first_line and len(first_line) < 200:
                    # Check if it looks like a title
                    if first_line.startswith('#') or len(first_line) < 100:
                        metadata["title"] = first_line.lstrip('#').strip()
        
        # Try to extract date
        if "date" in fields:
            date_patterns = [
                r'\d{4}[-/]\d{1,2}[-/]\d{1,2}',
                r'\d{1,2}[-/]\d{1,2}[-/]\d{4}',
            ]
            for pattern in date_patterns:
                match = re.search(pattern, text[:1000])
                if match:
                    metadata["date"] = match.group()
                    break
        
        # Word count
        metadata["word_count"] = len(text.split())
        metadata["char_count"] = len(text)
        
        return metadata


class BaseChunker(ABC):
    """Abstract base class for all chunking strategies"""
    
    def __init__(self, config: ChunkingConfig):
        self.config = config
    
    @abstractmethod
    def chunk(self, text: str) -> List[Chunk]:
        """Split text into chunks"""
        pass
    
    def _create_chunk(
        self, 
        text: str, 
        index: int, 
        metadata: Optional[Dict[str, Any]] = None,
        parent_id: Optional[str] = None
    ) -> Chunk:
        """Create a Chunk object with computed fields"""
        return Chunk(
            text=text.strip(),
            index=index,
            metadata=metadata or {},
            parent_id=parent_id,
        )
    
    def _split_with_overlap(self, text: str, chunk_size: int, overlap: int) -> List[str]:
        """Split text with overlap, trying to break at natural boundaries"""
        if not text or chunk_size <= 0:
            return []
        
        # Ensure overlap is reasonable
        overlap = min(overlap, chunk_size // 2)
        
        chunks = []
        start = 0
        text_len = len(text)
        
        while start < text_len:
            end = min(start + chunk_size, text_len)
            
            # Try to find a good break point near the end
            if end < text_len:
                # Look for sentence/paragraph breaks within the last 20% of chunk
                search_start = max(start, end - int(chunk_size * 0.2))
                search_text = text[search_start:end]
                
                # Priority: paragraph > sentence > word
                best_pos = -1
                for pattern in ['\n\n', '\n', '。', '.', '！', '!', '？', '?']:
                    pos = search_text.rfind(pattern)
                    if pos > 0:
                        best_pos = search_start + pos + len(pattern)
                        break
                
                if best_pos > start:
                    end = best_pos
            
            chunk_text = text[start:end].strip()
            if chunk_text and len(chunk_text) >= self.config.min_chunk_size:
                chunks.append(chunk_text)
            
            # Move start forward - ensure we make progress
            # The step should be at least (chunk_size - overlap) to avoid excessive overlap
            # The step should be at least (chunk_size - overlap) to avoid excessive overlap
            step = max(chunk_size - overlap, 1)
            new_start = start + step
            
            # If we didn't reach the end, use the actual end minus overlap
            if end < text_len:
                new_start = max(new_start, end - overlap)
            else:
                new_start = text_len  # We're done
            
            start = new_start
        
        return chunks


class FixedSizeChunker(BaseChunker):
    """Fixed size chunking with overlap"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        chunk_texts = self._split_with_overlap(
            text, 
            self.config.chunk_size, 
            self.config.chunk_overlap
        )
        
        return [
            self._create_chunk(t, i) 
            for i, t in enumerate(chunk_texts) 
            if t.strip()
        ]


class ParagraphChunker(BaseChunker):
    """Split by paragraphs, merging small ones"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        # Split by double newlines (paragraphs)
        paragraphs = re.split(r'\n\s*\n', text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]
        
        chunks = []
        current = ""
        index = 0
        
        for para in paragraphs:
            if len(current) + len(para) + 2 <= self.config.chunk_size:
                current = f"{current}\n\n{para}" if current else para
            else:
                if current:
                    chunks.append(self._create_chunk(current, index))
                    index += 1
                
                if len(para) > self.config.chunk_size:
                    # Split large paragraph
                    sub_chunks = self._split_with_overlap(
                        para, 
                        self.config.chunk_size, 
                        self.config.chunk_overlap
                    )
                    for sub in sub_chunks:
                        chunks.append(self._create_chunk(sub, index))
                        index += 1
                    current = ""
                else:
                    current = para
        
        if current:
            chunks.append(self._create_chunk(current, index))
        
        return chunks


class PageChunker(BaseChunker):
    """Split by page markers"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        # Use page marker (form feed or custom)
        marker = self.config.page_marker
        if marker == r"\f":
            pages = text.split('\f')
        else:
            pages = re.split(marker, text)
        
        chunks = []
        for i, page in enumerate(pages):
            page = page.strip()
            if not page:
                continue
            
            if len(page) <= self.config.chunk_size:
                chunks.append(self._create_chunk(page, i, {"page": i + 1}))
            else:
                # Split large pages
                sub_chunks = self._split_with_overlap(
                    page,
                    self.config.chunk_size,
                    self.config.chunk_overlap
                )
                for j, sub in enumerate(sub_chunks):
                    chunks.append(self._create_chunk(
                        sub, 
                        len(chunks), 
                        {"page": i + 1, "sub_chunk": j + 1}
                    ))
        
        return chunks



class HeadingChunker(BaseChunker):
    """Split by document headings/sections with recursive fallback"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        # Compile heading patterns
        patterns = self.config.heading_patterns or [
            r"^#{1,6}\s+.+$",
            r"^第[一二三四五六七八九十\d]+[章节条款]",
            r"^\d+\.\s+.+$",
        ]
        
        combined_pattern = '|'.join(f'({p})' for p in patterns)
        
        # Find all headings and their positions
        sections = []
        current_heading = None
        current_content = []
        
        for line in text.split('\n'):
            is_heading = bool(re.match(combined_pattern, line, re.MULTILINE))
            
            if is_heading:
                if current_heading is not None or current_content:
                    sections.append({
                        "heading": current_heading,
                        "content": '\n'.join(current_content)
                    })
                current_heading = line
                current_content = []
            else:
                current_content.append(line)
        
        # Add last section
        if current_heading is not None or current_content:
            sections.append({
                "heading": current_heading,
                "content": '\n'.join(current_content)
            })
        
        # Create chunks from sections
        chunks = []
        recursive_chunker = RecursiveChunker(self.config)
        
        for i, section in enumerate(sections):
            section_text = section["content"].strip()
            if section["heading"]:
                section_text = f"{section['heading']}\n\n{section_text}"
            
            if not section_text.strip():
                continue
            
            if len(section_text) <= self.config.chunk_size:
                chunks.append(self._create_chunk(
                    section_text, 
                    len(chunks),
                    {"heading": section["heading"]}
                ))
            else:
                # Split large sections using RecursiveChunker for better semantic preservation
                sub_chunks = recursive_chunker.chunk(section_text)
                for sub in sub_chunks:
                    # Update metadata with heading info
                    sub_meta = sub.metadata.copy()
                    sub_meta["heading"] = section["heading"]
                    
                    chunks.append(self._create_chunk(
                        sub.text,
                        len(chunks),
                        sub_meta
                    ))
        
        return chunks


class RegexChunker(BaseChunker):
    """Split by custom regex pattern"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        pattern = self.config.regex_pattern
        if not pattern:
            return RecursiveChunker(self.config).chunk(text)
        
        try:
            parts = re.split(pattern, text)
        except re.error:
            return RecursiveChunker(self.config).chunk(text)
        
        chunks = []
        recursive_chunker = RecursiveChunker(self.config)
        
        for i, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            
            if len(part) <= self.config.chunk_size:
                chunks.append(self._create_chunk(part, len(chunks)))
            else:
                sub_chunks = recursive_chunker.chunk(part)
                for sub in sub_chunks:
                    sub.index = len(chunks)
                    chunks.append(sub)
        
        return chunks


class SeparatorChunker(BaseChunker):
    """Split by custom separators with recursive fallback"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
        
        separators = self.config.separators or ["\n\n", "\n", " "]
        # Use primary separator first
        sep = self.config.primary_separator or separators[0]
        try:
            parts = text.split(sep)
        except Exception:
            return RecursiveChunker(self.config).chunk(text)
        
        chunks = []
        recursive_chunker = RecursiveChunker(self.config)
        
        for part in parts:
            part = part.strip()
            if not part:
                continue
                
            if len(part) <= self.config.chunk_size:
                chunks.append(self._create_chunk(part, len(chunks)))
            else:
                 sub_chunks = recursive_chunker.chunk(part)
                 for sub in sub_chunks:
                    sub.index = len(chunks)
                    chunks.append(sub)
        
        return chunks


class RecursiveChunker(BaseChunker):
    """Recursive character text splitter - splits hierarchically"""
    
    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []
            
        separators = self.config.separators or ["\n\n", "\n", "。", ".", " ", ""]
        return self._recursive_split(text, separators, 0)
    
    def _recursive_split(
        self, 
        text: str, 
        separators: List[str], 
        depth: int
    ) -> List[Chunk]:
        """Recursively split text using separator hierarchy"""
        if not text.strip():
            return []
        
        # Base case: text fits in chunk size
        if len(text) <= self.config.chunk_size:
            return [self._create_chunk(text, 0)]
        
        # No more separators: fall back to fixed size split
        if not separators:
            return [
                self._create_chunk(t, i) 
                for i, t in enumerate(self._split_with_overlap(
                    text, 
                    self.config.chunk_size, 
                    self.config.chunk_overlap
                ))
            ]
        
        sep = separators[0]
        remaining_seps = separators[1:]
        
        # Special case for empty separator (char split)
        if sep == "":
            return [
                self._create_chunk(t, i)
                for i, t in enumerate(self._split_with_overlap(
                    text,
                    self.config.chunk_size,
                    self.config.chunk_overlap
                ))
            ]
            
        # Try splitting with current separator
        parts = text.split(sep)
        
        # Include separator in chunks if desired, but for now simple split
        # We need to re-assemble parts into chunks < size
        
        chunks = []
        current_chunk_parts = []
        current_len = 0
        
        for part in parts:
            part_len = len(part)
            sep_len = len(sep)
            
            # If single part is too big, it needs to be processed recursively
            if part_len > self.config.chunk_size:
                # First, flush current buffer if any
                if current_chunk_parts:
                    completed_text = sep.join(current_chunk_parts)
                    chunks.append(self._create_chunk(completed_text, len(chunks)))
                    current_chunk_parts = []
                    current_len = 0
                
                # Recursively split this large part
                sub_chunks = self._recursive_split(part, remaining_seps, depth + 1)
                for sub in sub_chunks:
                    sub.index = len(chunks)
                    chunks.append(sub)
                    
            else:
                # If adding this part exceeds size, flush buffer
                if current_len + sep_len + part_len > self.config.chunk_size:
                    if current_chunk_parts:
                        completed_text = sep.join(current_chunk_parts)
                        chunks.append(self._create_chunk(completed_text, len(chunks)))
                    current_chunk_parts = [part]
                    current_len = part_len
                else:
                    if current_chunk_parts:
                        current_len += sep_len
                    current_chunk_parts.append(part)
                    current_len += part_len
                    
        # Flush final buffer
        if current_chunk_parts:
            completed_text = sep.join(current_chunk_parts)
            chunks.append(self._create_chunk(completed_text, len(chunks)))
            
        return chunks


class HierarchicalChunker(BaseChunker):
    """
    Parent-child hierarchical chunking (Small-to-Large retrieval).

    Best for:
    - Complex Q&A needing both precision and broad context
    - Long documents where answers are specific but context matters
    - Enterprise use cases with strict token budgets

    Strategy:
    - Index small "child" chunks for precision retrieval
    - Keep larger "parent" chunks for context when needed
    - Children are retrieved first, parent provides context
    """

    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []

        # First create parent chunks
        parent_config = ChunkingConfig(
            chunk_size=self.config.parent_chunk_size,
            chunk_overlap=self.config.parent_overlap,
            min_chunk_size=self.config.min_chunk_size,
            separators=self.config.separators,
        )

        if self.config.parent_mode == "full_doc":
            parents = [self._create_chunk(text, 0)]
        elif self.config.parent_mode == "section":
            parents = HeadingChunker(parent_config).chunk(text)
        elif self.config.parent_mode == "paragraph":
            parents = ParagraphChunker(parent_config).chunk(text)
        else:  # recursive (default)
            parents = RecursiveChunker(parent_config).chunk(text)

        # Create child chunks for each parent
        all_chunks = []

        # Child config with proper overlap
        child_config = ChunkingConfig(
            chunk_size=self.config.child_chunk_size,
            chunk_overlap=self.config.child_overlap,  # Use configured overlap
            min_chunk_size=max(50, self.config.min_chunk_size // 2),
            separators=self.config.separators,
        )
        child_chunker = RecursiveChunker(child_config)

        for parent_idx, parent in enumerate(parents):
            parent.index = len(all_chunks)  # Use global unique index instead of parent_idx
            parent.metadata["is_parent"] = True
            parent.metadata["chunk_type"] = "parent"
            all_chunks.append(parent)

            # Create children using recursive splitter on parent text
            children = child_chunker.chunk(parent.text)

            for child_idx, child in enumerate(children):
                child.index = len(all_chunks)
                child.metadata["is_child"] = True
                child.metadata["chunk_type"] = "child"
                child.metadata["parent_index"] = parent.index
                child.metadata["parent_hash"] = parent.hash_id
                child.metadata["child_position"] = child_idx
                child.parent_id = parent.hash_id

                parent.children.append(child)
                all_chunks.append(child)

        return all_chunks


class AutomaticChunker(BaseChunker):
    """
    Intelligent automatic chunking with content-aware strategy selection.

    Strategy Selection (Priority Order):
    1. Structured documents (headings) → HeadingChunker
    2. Long documents (>5000 chars) → HierarchicalChunker (parent-child)
    3. Documents with images → preserve image context
    4. Default → RecursiveChunker (85-90% recall, best general purpose)

    This follows industry best practices:
    - RecursiveCharacterTextSplitter for most documents
    - Parent-child for complex Q&A scenarios
    - Structure-aware for markdown/technical docs
    """

    # Image placeholder patterns
    IMAGE_PATTERNS = [
        r'\[Image\]',           # Our parser placeholder
        r'\[图片\]',            # Chinese placeholder
        r'!\[.*?\]\(.*?\)',     # Markdown images
        r'<img[^>]+>',          # HTML images
        r'\[IMAGE:.*?\]',       # Custom placeholder
    ]

    def chunk(self, text: str) -> List[Chunk]:
        if not text:
            return []

        # Analyze document characteristics
        has_headings = bool(re.search(
            r'^#{1,6}\s+|^第[一二三四五六七八九十\d]+[章节条款]|^\d+\.\d*\s+\w',
            text, re.MULTILINE
        ))
        has_images = any(re.search(p, text) for p in self.IMAGE_PATTERNS)
        doc_length = len(text)

        # Strategy 1: Structured documents with headings
        if has_headings:
            # Use heading-based chunking for structured documents
            return HeadingChunker(self.config).chunk(text)

        # Strategy 2: Long documents benefit from hierarchical chunking
        if doc_length > 5000:
            # Use parent-child for better context preservation
            return HierarchicalChunker(self.config).chunk(text)

        # Strategy 3: Handle documents with images
        if has_images and self.config.preserve_images:
            return self._chunk_with_image_awareness(text)

        # Strategy 4: Default to Recursive (best general purpose)
        return RecursiveChunker(self.config).chunk(text)

    def _chunk_with_image_awareness(self, text: str) -> List[Chunk]:
        """
        Chunk text while preserving image context.

        Images should not be split from their surrounding context.
        We identify image placeholders and ensure they stay with
        adjacent text for proper retrieval.
        """
        # Find all image positions
        image_positions = []
        for pattern in self.IMAGE_PATTERNS:
            for match in re.finditer(pattern, text):
                image_positions.append((match.start(), match.end()))

        if not image_positions:
            return RecursiveChunker(self.config).chunk(text)

        # Sort by position
        image_positions.sort(key=lambda x: x[0])

        # Create chunks ensuring images stay with context
        chunks = []
        current_pos = 0
        context_size = self.config.image_context_chars

        for img_start, img_end in image_positions:
            # Get text before this image (but after previous chunk)
            pre_text = text[current_pos:img_start].strip()

            if pre_text and len(pre_text) > self.config.min_chunk_size:
                # Chunk the pre-image text
                pre_chunks = RecursiveChunker(self.config).chunk(pre_text)
                for c in pre_chunks:
                    c.index = len(chunks)
                    chunks.append(c)

            # Create image chunk with surrounding context
            ctx_start = max(0, img_start - context_size)
            ctx_end = min(len(text), img_end + context_size)

            # Extend to sentence boundaries
            while ctx_start > 0 and text[ctx_start] not in '.。!！?？\n':
                ctx_start -= 1
            while ctx_end < len(text) and text[ctx_end - 1] not in '.。!！?？\n':
                ctx_end += 1

            image_chunk_text = text[ctx_start:ctx_end].strip()
            if image_chunk_text:
                chunk = self._create_chunk(
                    image_chunk_text,
                    len(chunks),
                    {"has_image": True, "chunk_type": "image_context"}
                )
                chunks.append(chunk)

            current_pos = ctx_end

        # Handle remaining text after last image
        if current_pos < len(text):
            remaining = text[current_pos:].strip()
            if remaining and len(remaining) > self.config.min_chunk_size:
                remaining_chunks = RecursiveChunker(self.config).chunk(remaining)
                for c in remaining_chunks:
                    c.index = len(chunks)
                    chunks.append(c)

        return chunks if chunks else RecursiveChunker(self.config).chunk(text)


def create_chunker(config: ChunkingConfig) -> BaseChunker:
    """Factory function to create appropriate chunker"""
    chunker_map = {
        ChunkingMode.AUTOMATIC: AutomaticChunker,
        ChunkingMode.FIXED_SIZE: FixedSizeChunker,
        ChunkingMode.PARAGRAPH: ParagraphChunker,
        ChunkingMode.PAGE: PageChunker,
        ChunkingMode.HEADING: HeadingChunker,
        ChunkingMode.REGEX: RegexChunker,
        ChunkingMode.SEPARATOR: SeparatorChunker,
        ChunkingMode.RECURSIVE: RecursiveChunker,
        ChunkingMode.HIERARCHICAL: HierarchicalChunker,
        ChunkingMode.QA: ParagraphChunker,
    }
    
    chunker_cls = chunker_map.get(config.mode, RecursiveChunker)
    return chunker_cls(config)


def process_document(
    text: str,
    config: ChunkingConfig,
    document_id: Optional[str] = None,
) -> List[Chunk]:
    """
    Main entry point for document processing.
    
    1. Preprocess text
    2. Extract metadata (optional)
    3. Chunk text
    4. Add document-level metadata to chunks
    """
    if not text:
        return []
    
    # Preprocess
    processed_text = TextPreprocessor.preprocess(text, config)
    
    if not processed_text:
        return []
    
    # Extract metadata
    doc_metadata = {}
    if config.extract_metadata:
        doc_metadata = TextPreprocessor.extract_metadata(processed_text, config.metadata_fields)
    
    # Chunk
    chunker = create_chunker(config)
    chunks = chunker.chunk(processed_text)
    
    # Add document-level metadata
    for chunk in chunks:
        if document_id:
            chunk.metadata["document_id"] = document_id
        chunk.metadata.update(doc_metadata)
    
    return chunks


def flatten_chunks(chunks: List[Chunk]) -> List[Chunk]:
    """
    Flatten hierarchical chunks to a single list.
    For hierarchical chunking, this returns only the leaf (child) chunks.
    """
    result = []
    for chunk in chunks:
        if chunk.children:
            # Has children, use children instead
            result.extend(flatten_chunks(chunk.children))
        else:
            result.append(chunk)
    return result


# Convenience functions
def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50, mode: str = "automatic") -> List[str]:
    """Simple interface to chunk text and return list of strings"""
    config = ChunkingConfig(
        mode=ChunkingMode(mode) if mode in [m.value for m in ChunkingMode] else ChunkingMode.AUTOMATIC,
        chunk_size=chunk_size,
        chunk_overlap=overlap,
    )
    chunks = process_document(text, config)
    return [c.text for c in chunks]
