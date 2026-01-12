"""
Document Image Extractor

Unified image extraction from various document formats:
- PDF (via PyMuPDF/fitz)
- DOCX (via python-docx)
- HTML (via BeautifulSoup)

Follows Dify 1.11 approach:
- Auto-extract images from documents
- Associate images with nearby text chunks
- Support images up to 2MB for embedding

Usage:
    extractor = DocumentImageExtractor()
    result = await extractor.extract("document.docx", content_bytes)
    for img in result.images:
        if img.is_embeddable:
            # Process image for embedding
            pass
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

# Supported image formats for multimodal embedding
EMBEDDABLE_IMAGE_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/bmp",
    "image/webp",
    "image/gif",
}

# Maximum image size for embedding (2MB - Dify standard)
MAX_IMAGE_SIZE_BYTES = 2 * 1024 * 1024

# Minimum image dimensions (skip tiny images)
MIN_IMAGE_WIDTH = 50
MIN_IMAGE_HEIGHT = 50

# Extension to MIME type mapping
EXTENSION_TO_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "jpe": "image/jpeg",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "gif": "image/gif",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ExtractedImage:
    """Represents an image extracted from a document."""

    image_id: str                    # Unique ID (hash of content)
    content: bytes                   # Raw image bytes
    mime_type: str                   # MIME type (image/png, etc.)
    width: int                       # Image width in pixels
    height: int                      # Image height in pixels
    source_location: str             # Where found (page number, section, etc.)
    context_text: str = ""           # Surrounding text context
    alt_text: str = ""               # Alt text if available
    filename: str = ""               # Original filename if known
    char_offset: int = -1            # Character offset in document
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def size_bytes(self) -> int:
        """Size of image in bytes."""
        return len(self.content)

    @property
    def is_embeddable(self) -> bool:
        """Check if image can be embedded."""
        return (
            self.mime_type in EMBEDDABLE_IMAGE_TYPES
            and self.size_bytes <= MAX_IMAGE_SIZE_BYTES
            and self.width >= MIN_IMAGE_WIDTH
            and self.height >= MIN_IMAGE_HEIGHT
        )

    @property
    def aspect_ratio(self) -> float:
        """Image aspect ratio (width/height)."""
        if self.height == 0:
            return 0.0
        return self.width / self.height

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (without content for serialization)."""
        return {
            "image_id": self.image_id,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "source_location": self.source_location,
            "size_bytes": self.size_bytes,
            "context_text": self.context_text[:200] if self.context_text else "",
            "alt_text": self.alt_text,
            "filename": self.filename,
            "char_offset": self.char_offset,
            "is_embeddable": self.is_embeddable,
            "aspect_ratio": round(self.aspect_ratio, 2),
            "metadata": self.metadata,
        }


@dataclass
class DocumentExtractionResult:
    """Result of document extraction containing text and images."""

    text: str                              # Extracted text content
    images: List[ExtractedImage]           # Extracted images
    document_type: str                     # pdf, docx, html, etc.
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def embeddable_images(self) -> List[ExtractedImage]:
        """Get only images that can be embedded."""
        return [img for img in self.images if img.is_embeddable]

    @property
    def total_images(self) -> int:
        return len(self.images)

    @property
    def embeddable_image_count(self) -> int:
        return len(self.embeddable_images)

    def associate_images_with_text(
        self,
        chunks: List[Dict[str, Any]],
        max_distance: int = 500,
    ) -> Dict[str, List[ExtractedImage]]:
        """Associate images with text chunks based on proximity.

        Args:
            chunks: List of text chunks with 'start' and 'end' char offsets
            max_distance: Maximum character distance for association

        Returns:
            Dict mapping chunk_id to list of associated images
        """
        associations: Dict[str, List[ExtractedImage]] = {}

        for img in self.embeddable_images:
            if img.char_offset < 0:
                continue

            for chunk in chunks:
                chunk_id = chunk.get("chunk_id", chunk.get("id", ""))
                chunk_start = chunk.get("start", 0)
                chunk_end = chunk.get("end", len(self.text))

                # Check if image is within or near this chunk
                if chunk_start - max_distance <= img.char_offset <= chunk_end + max_distance:
                    if chunk_id not in associations:
                        associations[chunk_id] = []
                    associations[chunk_id].append(img)

        return associations


# ============================================================
# Image Utility Functions
# ============================================================

def generate_image_id(content: bytes) -> str:
    """Generate unique ID for image based on content hash."""
    return hashlib.md5(content).hexdigest()[:16]


def detect_mime_type(content: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if len(content) < 8:
        return "application/octet-stream"

    if content[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    elif content[:2] == b'\xff\xd8':
        return "image/jpeg"
    elif content[:6] in (b'GIF87a', b'GIF89a'):
        return "image/gif"
    elif content[:2] == b'BM':
        return "image/bmp"
    elif content[:4] == b'RIFF' and len(content) > 12 and content[8:12] == b'WEBP':
        return "image/webp"
    elif content[:4] == b'II*\x00' or content[:4] == b'MM\x00*':
        return "image/tiff"
    return "application/octet-stream"


def get_image_dimensions(content: bytes, mime_type: str) -> Tuple[int, int]:
    """Get image dimensions without loading full image."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as img:
            return img.size
    except Exception:
        # Fallback: parse header manually for common formats
        return _parse_dimensions_from_header(content, mime_type)


def _parse_dimensions_from_header(content: bytes, mime_type: str) -> Tuple[int, int]:
    """Parse image dimensions from header bytes."""
    try:
        if mime_type == "image/png" and len(content) >= 24:
            # PNG: width at bytes 16-20, height at 20-24
            width = int.from_bytes(content[16:20], "big")
            height = int.from_bytes(content[20:24], "big")
            return width, height

        elif mime_type in ("image/jpeg", "image/jpg") and len(content) >= 4:
            # JPEG: Need to find SOF0 marker
            data = io.BytesIO(content)
            data.read(2)  # Skip SOI
            while True:
                marker = data.read(2)
                if len(marker) < 2:
                    break
                if marker[0] != 0xFF:
                    break
                if marker[1] in (0xC0, 0xC1, 0xC2):  # SOF markers
                    data.read(3)  # Skip length and precision
                    height = int.from_bytes(data.read(2), "big")
                    width = int.from_bytes(data.read(2), "big")
                    return width, height
                elif marker[1] == 0xD9:  # EOI
                    break
                else:
                    length = int.from_bytes(data.read(2), "big")
                    data.read(length - 2)
            return 0, 0

        elif mime_type == "image/gif" and len(content) >= 10:
            # GIF: width at bytes 6-8, height at 8-10
            width = int.from_bytes(content[6:8], "little")
            height = int.from_bytes(content[8:10], "little")
            return width, height

        elif mime_type == "image/bmp" and len(content) >= 26:
            # BMP: width at bytes 18-22, height at 22-26
            width = int.from_bytes(content[18:22], "little")
            height = abs(int.from_bytes(content[22:26], "little", signed=True))
            return width, height

    except Exception:
        pass

    return 0, 0


# ============================================================
# Document Type Extractors
# ============================================================

class PDFExtractor:
    """Extract images from PDF documents using PyMuPDF."""

    def __init__(
        self,
        min_width: int = MIN_IMAGE_WIDTH,
        min_height: int = MIN_IMAGE_HEIGHT,
        context_chars: int = 500,
    ):
        self.min_width = min_width
        self.min_height = min_height
        self.context_chars = context_chars

    async def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract text and images from PDF."""
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF not installed, using fallback")
            return await self._fallback_extract(content)

        return await asyncio.to_thread(self._extract_sync, content)

    def _extract_sync(self, content: bytes) -> DocumentExtractionResult:
        """Synchronous extraction (runs in thread)."""
        import fitz

        doc = fitz.open(stream=content, filetype="pdf")
        text_parts: List[str] = []
        images: List[ExtractedImage] = []
        seen_hashes: set = set()

        try:
            for page_num in range(len(doc)):
                page = doc[page_num]

                # Extract text
                page_text = page.get_text("text")
                text_parts.append(page_text)

                # Extract images
                for img_index, img in enumerate(page.get_images(full=True)):
                    try:
                        xref = img[0]
                        base_img = doc.extract_image(xref)

                        if not base_img:
                            continue

                        img_bytes = base_img["image"]
                        img_ext = base_img.get("ext", "png")
                        mime_type = EXTENSION_TO_MIME.get(img_ext, "image/png")

                        # Deduplicate by hash
                        img_hash = hashlib.md5(img_bytes).hexdigest()
                        if img_hash in seen_hashes:
                            continue
                        seen_hashes.add(img_hash)

                        # Get dimensions
                        width = base_img.get("width", 0)
                        height = base_img.get("height", 0)

                        # Skip small images
                        if width < self.min_width or height < self.min_height:
                            continue

                        # Get context text
                        context = page_text[:self.context_chars] if page_text else ""

                        # Calculate approximate char offset
                        char_offset = sum(len(t) for t in text_parts[:-1])

                        images.append(ExtractedImage(
                            image_id=generate_image_id(img_bytes),
                            content=img_bytes,
                            mime_type=mime_type,
                            width=width,
                            height=height,
                            source_location=f"page_{page_num + 1}",
                            context_text=context,
                            char_offset=char_offset,
                            metadata={
                                "page_number": page_num + 1,
                                "image_index": img_index,
                                "xref": xref,
                            },
                        ))

                    except Exception as e:
                        logger.debug(f"Failed to extract image from page {page_num}: {e}")

            return DocumentExtractionResult(
                text="\n\n".join(text_parts),
                images=images,
                document_type="pdf",
                metadata={
                    "page_count": len(doc),
                    "total_images_found": len(images),
                },
            )

        finally:
            doc.close()

    async def _fallback_extract(self, content: bytes) -> DocumentExtractionResult:
        """Fallback using pypdf if PyMuPDF not available."""
        try:
            from pypdf import PdfReader
        except ImportError:
            return DocumentExtractionResult(
                text="",
                images=[],
                document_type="pdf",
                metadata={"error": "No PDF library available"},
            )

        reader = PdfReader(io.BytesIO(content))
        text_parts = [page.extract_text() or "" for page in reader.pages]

        return DocumentExtractionResult(
            text="\n\n".join(text_parts),
            images=[],  # pypdf doesn't easily extract images
            document_type="pdf",
            metadata={"page_count": len(reader.pages)},
        )


class DOCXExtractor:
    """Extract images from DOCX documents using python-docx."""

    def __init__(
        self,
        min_width: int = MIN_IMAGE_WIDTH,
        min_height: int = MIN_IMAGE_HEIGHT,
        context_chars: int = 500,
    ):
        self.min_width = min_width
        self.min_height = min_height
        self.context_chars = context_chars

    async def extract(self, content: bytes) -> DocumentExtractionResult:
        """Extract text and images from DOCX."""
        try:
            from docx import Document
            from docx.opc.constants import RELATIONSHIP_TYPE as RT
        except ImportError:
            return DocumentExtractionResult(
                text="",
                images=[],
                document_type="docx",
                metadata={"error": "python-docx not installed"},
            )

        return await asyncio.to_thread(self._extract_sync, content)

    def _extract_sync(self, content: bytes) -> DocumentExtractionResult:
        """Synchronous extraction."""
        from docx import Document
        from docx.opc.constants import RELATIONSHIP_TYPE as RT

        doc = Document(io.BytesIO(content))
        text_parts: List[str] = []
        images: List[ExtractedImage] = []
        seen_hashes: set = set()

        # Extract text from paragraphs
        char_offset = 0
        for para in doc.paragraphs:
            text_parts.append(para.text)
            char_offset += len(para.text) + 1

        # Extract images from document relationships
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                try:
                    img_part = rel.target_part
                    img_bytes = img_part.blob

                    # Deduplicate
                    img_hash = hashlib.md5(img_bytes).hexdigest()
                    if img_hash in seen_hashes:
                        continue
                    seen_hashes.add(img_hash)

                    # Detect type and dimensions
                    mime_type = detect_mime_type(img_bytes)
                    width, height = get_image_dimensions(img_bytes, mime_type)

                    # Skip small images
                    if width < self.min_width or height < self.min_height:
                        continue

                    # Get filename from content type
                    filename = getattr(img_part, "partname", "").split("/")[-1]

                    images.append(ExtractedImage(
                        image_id=generate_image_id(img_bytes),
                        content=img_bytes,
                        mime_type=mime_type,
                        width=width,
                        height=height,
                        source_location="document_body",
                        filename=filename,
                        metadata={"relationship_id": rel.rId},
                    ))

                except Exception as e:
                    logger.debug(f"Failed to extract DOCX image: {e}")

        full_text = "\n".join(text_parts)

        return DocumentExtractionResult(
            text=full_text,
            images=images,
            document_type="docx",
            metadata={
                "paragraph_count": len(doc.paragraphs),
                "total_images_found": len(images),
            },
        )


class HTMLExtractor:
    """Extract images from HTML documents using BeautifulSoup."""

    def __init__(
        self,
        min_width: int = MIN_IMAGE_WIDTH,
        min_height: int = MIN_IMAGE_HEIGHT,
        context_chars: int = 500,
        download_images: bool = False,
        base_url: Optional[str] = None,
    ):
        self.min_width = min_width
        self.min_height = min_height
        self.context_chars = context_chars
        self.download_images = download_images
        self.base_url = base_url

    async def extract(self, content: Union[bytes, str]) -> DocumentExtractionResult:
        """Extract text and images from HTML."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return DocumentExtractionResult(
                text="",
                images=[],
                document_type="html",
                metadata={"error": "BeautifulSoup not installed"},
            )

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="ignore")

        soup = BeautifulSoup(content, "html.parser")

        # Extract text
        for script in soup(["script", "style", "noscript"]):
            script.decompose()
        text = soup.get_text(separator="\n", strip=True)

        # Extract images
        images: List[ExtractedImage] = []
        seen_hashes: set = set()

        for img_tag in soup.find_all("img"):
            try:
                src = img_tag.get("src", "")
                alt = img_tag.get("alt", "")

                if not src:
                    continue

                # Handle data URIs
                if src.startswith("data:"):
                    img_data = self._parse_data_uri(src)
                    if img_data:
                        content_bytes, mime_type = img_data

                        # Deduplicate
                        img_hash = hashlib.md5(content_bytes).hexdigest()
                        if img_hash in seen_hashes:
                            continue
                        seen_hashes.add(img_hash)

                        width, height = get_image_dimensions(content_bytes, mime_type)

                        if width >= self.min_width and height >= self.min_height:
                            # Get surrounding text
                            context = self._get_surrounding_text(img_tag, self.context_chars)

                            images.append(ExtractedImage(
                                image_id=generate_image_id(content_bytes),
                                content=content_bytes,
                                mime_type=mime_type,
                                width=width,
                                height=height,
                                source_location="inline_data_uri",
                                alt_text=alt,
                                context_text=context,
                            ))

                elif self.download_images:
                    # Download external images (async)
                    img_result = await self._download_image(src)
                    if img_result:
                        content_bytes, mime_type = img_result

                        img_hash = hashlib.md5(content_bytes).hexdigest()
                        if img_hash in seen_hashes:
                            continue
                        seen_hashes.add(img_hash)

                        width, height = get_image_dimensions(content_bytes, mime_type)

                        if width >= self.min_width and height >= self.min_height:
                            context = self._get_surrounding_text(img_tag, self.context_chars)
                            filename = src.split("/")[-1].split("?")[0]

                            images.append(ExtractedImage(
                                image_id=generate_image_id(content_bytes),
                                content=content_bytes,
                                mime_type=mime_type,
                                width=width,
                                height=height,
                                source_location=src,
                                alt_text=alt,
                                filename=filename,
                                context_text=context,
                            ))

            except Exception as e:
                logger.debug(f"Failed to extract HTML image: {e}")

        return DocumentExtractionResult(
            text=text,
            images=images,
            document_type="html",
            metadata={
                "total_img_tags": len(soup.find_all("img")),
                "extracted_images": len(images),
            },
        )

    def _parse_data_uri(self, data_uri: str) -> Optional[Tuple[bytes, str]]:
        """Parse a data URI into bytes and MIME type."""
        match = re.match(r"data:([^;,]+)?(?:;base64)?,(.+)", data_uri)
        if not match:
            return None

        mime_type = match.group(1) or "application/octet-stream"
        data = match.group(2)

        try:
            content = base64.b64decode(data)
            return content, mime_type
        except Exception:
            return None

    def _get_surrounding_text(self, element, max_chars: int) -> str:
        """Get text surrounding an element."""
        parts = []

        # Get preceding text
        prev = element.find_previous(string=True)
        if prev:
            parts.append(str(prev).strip()[:max_chars // 2])

        # Get following text
        next_elem = element.find_next(string=True)
        if next_elem:
            parts.append(str(next_elem).strip()[:max_chars // 2])

        return " ".join(parts)

    async def _download_image(self, url: str) -> Optional[Tuple[bytes, str]]:
        """Download an image from URL."""
        try:
            import httpx

            # Resolve relative URLs
            if self.base_url and not url.startswith(("http://", "https://")):
                url = urljoin(self.base_url, url)

            if not url.startswith(("http://", "https://")):
                return None

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    content = response.content
                    mime_type = response.headers.get("content-type", "").split(";")[0]
                    if not mime_type.startswith("image/"):
                        mime_type = detect_mime_type(content)
                    return content, mime_type

        except Exception as e:
            logger.debug(f"Failed to download image {url}: {e}")

        return None


# ============================================================
# Unified Document Image Extractor
# ============================================================

class DocumentImageExtractor:
    """Unified image extractor supporting multiple document formats.

    Supported formats:
    - PDF (.pdf) - via PyMuPDF
    - DOCX (.docx) - via python-docx
    - HTML (.html, .htm) - via BeautifulSoup

    Usage:
        extractor = DocumentImageExtractor()

        # Extract from bytes
        result = await extractor.extract("document.pdf", pdf_bytes)

        # Get embeddable images
        for img in result.embeddable_images:
            print(f"Image: {img.image_id}, Size: {img.size_bytes}")

        # Associate images with text chunks
        associations = result.associate_images_with_text(chunks)
    """

    def __init__(
        self,
        min_width: int = MIN_IMAGE_WIDTH,
        min_height: int = MIN_IMAGE_HEIGHT,
        context_chars: int = 500,
        download_html_images: bool = False,
    ):
        """Initialize the extractor.

        Args:
            min_width: Minimum image width to extract
            min_height: Minimum image height to extract
            context_chars: Characters of context to extract
            download_html_images: Whether to download external images in HTML
        """
        self.min_width = min_width
        self.min_height = min_height
        self.context_chars = context_chars
        self.download_html_images = download_html_images

        # Initialize specialized extractors
        self._pdf_extractor = PDFExtractor(min_width, min_height, context_chars)
        self._docx_extractor = DOCXExtractor(min_width, min_height, context_chars)
        self._html_extractor = HTMLExtractor(
            min_width, min_height, context_chars,
            download_images=download_html_images,
        )

    def _detect_document_type(self, filename: str, content: bytes) -> str:
        """Detect document type from filename or content."""
        ext = Path(filename).suffix.lower() if filename else ""

        if ext == ".pdf" or content[:4] == b"%PDF":
            return "pdf"
        elif ext == ".docx" or content[:4] == b"PK\x03\x04":
            return "docx"
        elif ext in (".html", ".htm") or b"<html" in content[:1000].lower():
            return "html"
        else:
            return "unknown"

    async def extract(
        self,
        filename: str,
        content: bytes,
        document_type: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> DocumentExtractionResult:
        """Extract text and images from a document.

        Args:
            filename: Document filename (used for type detection)
            content: Document content bytes
            document_type: Override auto-detected type (pdf, docx, html)
            base_url: Base URL for resolving relative image URLs (HTML only)

        Returns:
            DocumentExtractionResult with text and images
        """
        doc_type = document_type or self._detect_document_type(filename, content)

        if doc_type == "pdf":
            return await self._pdf_extractor.extract(content)

        elif doc_type == "docx":
            return await self._docx_extractor.extract(content)

        elif doc_type == "html":
            if base_url:
                self._html_extractor.base_url = base_url
            return await self._html_extractor.extract(content)

        else:
            logger.warning(f"Unsupported document type: {doc_type}")
            return DocumentExtractionResult(
                text="",
                images=[],
                document_type=doc_type,
                metadata={"error": f"Unsupported document type: {doc_type}"},
            )

    async def extract_from_file(
        self,
        file_path: Union[str, Path],
        base_url: Optional[str] = None,
    ) -> DocumentExtractionResult:
        """Extract from a file path.

        Args:
            file_path: Path to the document file
            base_url: Base URL for HTML images

        Returns:
            DocumentExtractionResult
        """
        path = Path(file_path)
        content = path.read_bytes()
        return await self.extract(path.name, content, base_url=base_url)


# ============================================================
# Convenience Functions
# ============================================================

async def extract_images_from_document(
    filename: str,
    content: bytes,
    min_size: int = 50,
) -> List[ExtractedImage]:
    """Convenience function to extract embeddable images from a document.

    Args:
        filename: Document filename
        content: Document content bytes
        min_size: Minimum image dimension

    Returns:
        List of embeddable images
    """
    extractor = DocumentImageExtractor(min_width=min_size, min_height=min_size)
    result = await extractor.extract(filename, content)
    return result.embeddable_images
