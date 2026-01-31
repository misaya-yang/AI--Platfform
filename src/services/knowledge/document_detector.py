"""
Document Type Detector - Intelligent document classification

Automatically detects document type and recommends the optimal processing mode:
- native_pdf: Digital PDF with text layer -> text_only
- scanned_pdf: Scanned PDF (image-only) -> scanned
- mixed_pdf: Mixed content (some pages text, some images) -> multimodal
- Other formats (docx, txt, markdown, html, image)

Detection is performed by sampling the first few pages to minimize I/O.
"""

from dataclasses import dataclass, field
from enum import Enum
import os
from typing import Any, Dict, List, Optional, Tuple, Union

from ...config.settings import settings
from ...core.observability.logging import get_logger
from .processing_mode import ProcessingMode

logger = get_logger(__name__)


class DocumentType(str, Enum):
    """Detected document types."""
    
    NATIVE_PDF = "native_pdf"       # Digital PDF with text layer
    SCANNED_PDF = "scanned_pdf"     # Scanned PDF (pure images)
    MIXED_PDF = "mixed_pdf"         # Mixed: some pages text, some images
    DOCX = "docx"                   # Microsoft Word
    TXT = "txt"                     # Plain text
    MARKDOWN = "markdown"           # Markdown
    HTML = "html"                   # HTML
    IMAGE = "image"                 # Image file (jpg, png, etc.)
    UNKNOWN = "unknown"             # Unknown type


@dataclass
class DetectionResult:
    """Result of document type detection."""
    
    document_type: DocumentType
    recommended_mode: ProcessingMode
    confidence: float  # 0.0 - 1.0
    file_size: int
    page_count: Optional[int] = None
    has_images: bool = False
    text_coverage: float = 0.0  # 0-1, ratio of pages with text
    is_large_file: bool = False  # > 50MB
    sample_pages_checked: int = 0
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "document_type": self.document_type.value,
            "recommended_mode": self.recommended_mode.value,
            "confidence": self.confidence,
            "file_size": self.file_size,
            "page_count": self.page_count,
            "has_images": self.has_images,
            "text_coverage": self.text_coverage,
            "is_large_file": self.is_large_file,
            "sample_pages_checked": self.sample_pages_checked,
            "details": self.details,
        }


class DocumentTypeDetector:
    """
    Intelligent document type detector.
    
    Analyzes document content to determine optimal processing mode.
    Uses sampling (first N pages) for large files to minimize I/O.
    """
    
    # Thresholds from configuration
    LARGE_FILE_THRESHOLD = settings.knowledge.large_file_threshold
    SAMPLE_PAGES = settings.knowledge.detection_sample_pages
    
    # Text coverage thresholds for PDF classification
    NATIVE_PDF_THRESHOLD = settings.knowledge.detection_native_pdf_threshold
    SCANNED_PDF_THRESHOLD = settings.knowledge.detection_scanned_pdf_threshold
    
    # Minimum characters per page to consider it "has text"
    MIN_CHARS_PER_PAGE = settings.knowledge.detection_min_chars_per_page
    
    # Maximum content size for memory processing
    MAX_MEMORY_SIZE = settings.knowledge.max_memory_processing_size
    
    def __init__(self):
        """Initialize detector."""
        self._fitz = None
    
    def _get_fitz(self):
        """Lazy load PyMuPDF."""
        if self._fitz is None:
            try:
                import fitz
                self._fitz = fitz
            except ImportError:
                raise ImportError(
                    "PyMuPDF (fitz) is required for document type detection. "
                    "Install with: pip install 'ai-gateway[documents]' or pip install pymupdf"
                )
        return self._fitz
    
    async def detect(
        self,
        content: Union[bytes, str],
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> DetectionResult:
        """
        Detect document type and recommend processing mode.
        
        Args:
            content: File content as bytes or file path
            filename: Original filename (for extension-based detection)
            mime_type: MIME type if known
            
        Returns:
            DetectionResult with type, recommended mode, and metadata
        """
        file_size = self._resolve_file_size(content)
        
        # FIX: Check content size for memory-based processing
        if isinstance(content, bytes) and len(content) > self.MAX_MEMORY_SIZE:
            raise ValueError(
                f"Content too large for memory processing ({len(content)} bytes). "
                f"Maximum allowed: {self.MAX_MEMORY_SIZE} bytes. Use file path instead."
            )
        
        is_large = file_size > self.LARGE_FILE_THRESHOLD
        
        # Normalize filename
        name = (filename or "").strip().lower()
        mime = (mime_type or "").strip().lower()
        
        # Detect by extension/mime type first
        if name.endswith(".pdf") or "application/pdf" in mime:
            return await self._detect_pdf(content, file_size, is_large)
        
        elif name.endswith(".docx") or "officedocument.wordprocessing" in mime:
            return self._create_result(
                doc_type=DocumentType.DOCX,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.95,
                file_size=file_size,
                is_large=is_large,
            )
        
        elif name.endswith(".txt") or "text/plain" in mime:
            return self._create_result(
                doc_type=DocumentType.TXT,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.99,
                file_size=file_size,
                is_large=is_large,
            )
        
        elif name.endswith(".md") or name.endswith(".markdown"):
            return self._create_result(
                doc_type=DocumentType.MARKDOWN,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.99,
                file_size=file_size,
                is_large=is_large,
            )
        
        elif name.endswith(".html") or name.endswith(".htm") or "text/html" in mime:
            return self._create_result(
                doc_type=DocumentType.HTML,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.95,
                file_size=file_size,
                is_large=is_large,
            )
        
        elif any(name.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]):
            return self._create_result(
                doc_type=DocumentType.IMAGE,
                mode=ProcessingMode.SCANNED,
                confidence=0.99,
                file_size=file_size,
                is_large=is_large,
            )
        
        else:
            # Try to detect PDF by magic bytes
            if self._is_pdf_magic(content):
                return await self._detect_pdf(content, file_size, is_large)
            
            # Unknown type, default to text processing
            return self._create_result(
                doc_type=DocumentType.UNKNOWN,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.5,
                file_size=file_size,
                is_large=is_large,
            )
    
    async def _detect_pdf(
        self,
        content: Union[bytes, str],
        file_size: int,
        is_large: bool,
    ) -> DetectionResult:
        """
        Detect PDF type by analyzing page content.
        
        Samples first N pages to determine if PDF is:
        - Native (has text layer)
        - Scanned (image-only)
        - Mixed (some pages text, some images)
        """
        fitz = self._get_fitz()
        doc = None
        
        try:
            # Open PDF document
            if isinstance(content, str):
                doc = fitz.open(content)
            else:
                doc = fitz.open(stream=content, filetype="pdf")
            
            total_pages = len(doc)
            pages_to_check = min(self.SAMPLE_PAGES, total_pages)
            
            pages_with_text = 0
            pages_with_images = 0
            total_chars = 0
            
            # Sample pages (first N + some from middle/end for better coverage)
            sample_indices = self._get_sample_indices(total_pages, pages_to_check)
            
            for page_idx in sample_indices:
                page = doc[page_idx]
                
                # Check text content
                text = page.get_text("text").strip()
                char_count = len(text)
                total_chars += char_count
                
                if char_count >= self.MIN_CHARS_PER_PAGE:
                    pages_with_text += 1
                
                # Check for images
                image_list = page.get_images()
                if image_list:
                    pages_with_images += 1
            
            # Calculate metrics
            text_coverage = pages_with_text / pages_to_check if pages_to_check > 0 else 0
            has_images = pages_with_images > 0
            
            # Determine type and mode
            if text_coverage >= self.NATIVE_PDF_THRESHOLD:
                doc_type = DocumentType.NATIVE_PDF
                mode = ProcessingMode.MULTIMODAL if has_images else ProcessingMode.TEXT_ONLY
                confidence = 0.9
            elif text_coverage <= self.SCANNED_PDF_THRESHOLD:
                doc_type = DocumentType.SCANNED_PDF
                mode = ProcessingMode.SCANNED
                confidence = 0.9
            else:
                doc_type = DocumentType.MIXED_PDF
                mode = ProcessingMode.MULTIMODAL
                confidence = 0.8
            
            return DetectionResult(
                document_type=doc_type,
                recommended_mode=mode,
                confidence=confidence,
                file_size=file_size,
                page_count=total_pages,
                has_images=has_images,
                text_coverage=text_coverage,
                is_large_file=is_large,
                sample_pages_checked=pages_to_check,
                details={
                    "pages_with_text": pages_with_text,
                    "pages_with_images": pages_with_images,
                    "total_chars_sampled": total_chars,
                    "sample_indices": sample_indices,
                },
            )
            
        except Exception as e:
            logger.error(f"Error during PDF detection: {e}")
            return self._create_result(
                doc_type=DocumentType.UNKNOWN,
                mode=ProcessingMode.TEXT_ONLY,
                confidence=0.3,
                file_size=file_size,
                is_large=is_large,
            )
        finally:
            # FIX: Ensure document is always closed in finally block
            if doc:
                try:
                    doc.close()
                except Exception:
                    pass
    
    def _get_sample_indices(self, total_pages: int, sample_count: int) -> List[int]:
        """
        Get page indices to sample for detection.
        
        Samples from beginning, middle, and end for better coverage.
        """
        if total_pages <= sample_count:
            return list(range(total_pages))
        
        indices = []
        
        # First few pages
        first_count = min(3, sample_count)
        indices.extend(range(first_count))
        
        remaining = sample_count - first_count
        if remaining > 0:
            # Middle page
            middle = total_pages // 2
            if middle not in indices:
                indices.append(middle)
                remaining -= 1
            
            # Last few pages
            if remaining > 0:
                for i in range(1, remaining + 1):
                    idx = total_pages - i
                    if idx not in indices and idx >= 0:
                        indices.append(idx)
        
        return sorted(indices)
    
    def _create_result(
        self,
        doc_type: DocumentType,
        mode: ProcessingMode,
        confidence: float,
        file_size: int,
        is_large: bool,
        **kwargs,
    ) -> DetectionResult:
        """Create a detection result with given parameters."""
        return DetectionResult(
            document_type=doc_type,
            recommended_mode=mode,
            confidence=confidence,
            file_size=file_size,
            is_large_file=is_large,
            **kwargs,
        )

    @staticmethod
    def _resolve_file_size(content: Union[bytes, str]) -> int:
        if isinstance(content, str):
            try:
                return os.path.getsize(content)
            except OSError:
                return 0
        return len(content)

    @staticmethod
    def _is_pdf_magic(content: Union[bytes, str]) -> bool:
        if isinstance(content, str):
            try:
                with open(content, "rb") as handle:
                    return handle.read(4) == b"%PDF"
            except OSError:
                return False
        return content[:4] == b"%PDF"
    
    @staticmethod
    def get_mode_display_name(mode: ProcessingMode) -> str:
        """Get human-readable name for processing mode."""
        names = {
            ProcessingMode.TEXT_ONLY: "Pure Text (OCR + Text Embedding)",
            ProcessingMode.SCANNED: "Scanned Document (Vision Embedding)",
            ProcessingMode.MULTIMODAL: "Multimodal (Text + Image Embedding)",
        }
        return names.get(mode, str(mode.value))
