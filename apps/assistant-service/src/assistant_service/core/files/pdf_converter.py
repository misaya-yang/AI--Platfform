"""
PDF to Images Converter for Vision Model Processing.

Converts PDF pages to images using PyMuPDF (fitz) for accurate document
analysis with vision models. This approach preserves:
- Table structures
- Embedded images
- Charts and diagrams
- Page layout and formatting

Industry standard approach used by Claude, GPT-4V, and Gemini.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ai_gateway_core.logging import get_logger


def import_pymupdf():
    """Import PyMuPDF with compatibility for old/new module names.

    Returns the ``fitz`` module regardless of whether the package was
    installed as ``pymupdf`` (modern) or ``fitz`` (legacy).
    """
    try:
        import pymupdf as fitz  # type: ignore
        return fitz
    except ImportError:
        import fitz  # type: ignore
        return fitz


logger = get_logger(__name__)


class PDFConversionError(Exception):
    """Raised when PDF conversion fails."""

    def __init__(
        self,
        message: str,
        file_path: str,
        original_error: Exception | None = None,
    ):
        self.file_path = file_path
        self.original_error = original_error
        super().__init__(message)


@dataclass
class PDFPageImage:
    """Represents a single PDF page converted to an image."""

    page_number: int  # 1-indexed
    base64_data: str
    media_type: str  # Always "image/png"
    width: int
    height: int
    size_bytes: int

    def to_openai_format(self) -> dict:
        """Convert to OpenAI vision API format."""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type};base64,{self.base64_data}",
            },
        }

    def to_anthropic_format(self) -> dict:
        """Convert to Anthropic vision API format."""
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": self.media_type,
                "data": self.base64_data,
            },
        }


@dataclass
class PDFConversionResult:
    """Result of PDF to images conversion."""

    file_path: str
    total_pages: int
    page_images: list[PDFPageImage] = field(default_factory=list)
    total_size_bytes: int = 0
    conversion_time_ms: float = 0

    @property
    def has_images(self) -> bool:
        """Check if conversion produced any images."""
        return len(self.page_images) > 0


# Type alias for progress callback
ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class PDFConverter:
    """
    Converts PDF pages to images for vision model processing.

    Uses PyMuPDF (fitz) for fast, high-quality PDF rendering.
    Each page is converted to a PNG image at configurable DPI.

    Example:
        converter = PDFConverter(dpi=150)
        result = await converter.convert(
            file_path="/path/to/document.pdf",
            max_pages=10,
            on_progress=lambda current, total, msg: print(f"{msg} ({current}/{total})")
        )

        for page in result.page_images:
            # Send to vision model
            content.append(page.to_openai_format())
    """

    # Default settings based on Gemini's approach:
    # - Pages scaled to max 3072x3072
    # - ~258 tokens per page
    DEFAULT_DPI = 150  # Good balance of quality vs size
    MAX_DPI = 300  # Maximum for high-detail documents
    MIN_DPI = 72  # Minimum for very large documents

    # Size limits
    MAX_PAGE_DIMENSION = 3072  # Max pixels per dimension (Gemini standard)
    TARGET_PAGE_SIZE_KB = 500  # Target ~500KB per page for efficiency

    def __init__(
        self,
        dpi: int = DEFAULT_DPI,
        max_dimension: int = MAX_PAGE_DIMENSION,
    ):
        """
        Initialize PDF converter.

        Args:
            dpi: Resolution for rendering (default 150)
            max_dimension: Maximum pixels per dimension (default 3072)
        """
        self.dpi = max(self.MIN_DPI, min(dpi, self.MAX_DPI))
        self.max_dimension = max_dimension

    @staticmethod
    def _import_pymupdf():
        """Import PyMuPDF with compatibility for old/new module names."""
        return import_pymupdf()

    def _calculate_zoom(self, page_width: float, page_height: float) -> float:
        """
        Calculate zoom factor to fit within max dimensions while maintaining aspect ratio.

        Args:
            page_width: PDF page width in points
            page_height: PDF page height in points

        Returns:
            Zoom factor for rendering
        """
        # Base zoom from DPI (72 points = 1 inch)
        base_zoom = self.dpi / 72.0

        # Calculate resulting pixel dimensions
        pixel_width = page_width * base_zoom
        pixel_height = page_height * base_zoom

        # Scale down if exceeds max dimension
        max_dim = max(pixel_width, pixel_height)
        if max_dim > self.max_dimension:
            scale_factor = self.max_dimension / max_dim
            return base_zoom * scale_factor

        return base_zoom

    async def convert(
        self,
        file_path: str,
        max_pages: int | None = None,
        page_range: tuple[int, int] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> PDFConversionResult:
        """
        Convert PDF to images.

        Args:
            file_path: Path to the PDF file
            max_pages: Maximum number of pages to convert (default: all)
            page_range: Specific page range (start, end) 1-indexed, inclusive
            on_progress: Async callback for progress updates (current, total, message)

        Returns:
            PDFConversionResult with converted page images

        Raises:
            PDFConversionError: If conversion fails
        """
        import time

        start_time = time.time()

        path = Path(file_path)
        if not path.exists():
            raise PDFConversionError(f"File not found: {file_path}", file_path)

        if path.suffix.lower() != ".pdf":
            raise PDFConversionError(f"Not a PDF file: {file_path}", file_path)

        try:
            fitz = self._import_pymupdf()

            # Open PDF
            doc = fitz.open(str(path))
            total_pages = len(doc)

            logger.info(f"[PDFConverter] Opening PDF: {file_path}, pages={total_pages}")

            # Determine pages to convert
            if page_range:
                start_page = max(0, page_range[0] - 1)  # Convert to 0-indexed
                end_page = min(total_pages, page_range[1])
            else:
                start_page = 0
                end_page = total_pages

            if max_pages:
                end_page = min(end_page, start_page + max_pages)

            pages_to_convert = end_page - start_page

            result = PDFConversionResult(
                file_path=file_path,
                total_pages=total_pages,
            )

            # Convert each page
            for i, page_num in enumerate(range(start_page, end_page)):
                page = doc[page_num]

                # Calculate zoom for optimal size
                zoom = self._calculate_zoom(page.rect.width, page.rect.height)
                matrix = fitz.Matrix(zoom, zoom)

                # Render page to PNG
                pix = page.get_pixmap(matrix=matrix, alpha=False)

                # Convert to PNG bytes
                png_bytes = pix.tobytes("png")
                size_bytes = len(png_bytes)

                # Convert to base64
                b64_data = base64.b64encode(png_bytes).decode("utf-8")

                page_image = PDFPageImage(
                    page_number=page_num + 1,  # 1-indexed for display
                    base64_data=b64_data,
                    media_type="image/png",
                    width=pix.width,
                    height=pix.height,
                    size_bytes=size_bytes,
                )

                result.page_images.append(page_image)
                result.total_size_bytes += size_bytes

                # Progress callback
                if on_progress:
                    await on_progress(
                        i + 1, pages_to_convert, f"Converting page {page_num + 1}/{total_pages}"
                    )

                logger.debug(
                    f"[PDFConverter] Converted page {page_num + 1}: "
                    f"{pix.width}x{pix.height}, {size_bytes / 1024:.1f}KB"
                )

            doc.close()

            result.conversion_time_ms = (time.time() - start_time) * 1000

            logger.info(
                f"[PDFConverter] Conversion complete: {len(result.page_images)} pages, "
                f"{result.total_size_bytes / 1024 / 1024:.2f}MB, "
                f"{result.conversion_time_ms:.0f}ms"
            )

            return result

        except ImportError as e:
            logger.error(f"[PDFConverter] PyMuPDF not installed: {e}")
            raise PDFConversionError(
                "PDF conversion requires PyMuPDF. Install with: pip install pymupdf",
                file_path=file_path,
                original_error=e,
            )

        except Exception as e:
            logger.error(
                f"[PDFConverter] Failed to convert {file_path}: {e}",
                exc_info=True,
            )
            raise PDFConversionError(
                f"Failed to convert PDF: {str(e)}",
                file_path=file_path,
                original_error=e,
            )

    async def convert_to_content_blocks(
        self,
        file_path: str,
        provider: str = "openai",
        max_pages: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[dict]:
        """
        Convert PDF to provider-specific content blocks ready for API.

        Args:
            file_path: Path to the PDF file
            provider: Model provider ("openai", "anthropic", "gemini")
            max_pages: Maximum pages to convert
            on_progress: Progress callback

        Returns:
            List of content blocks in provider format
        """
        result = await self.convert(
            file_path=file_path,
            max_pages=max_pages,
            on_progress=on_progress,
        )

        content_blocks = []

        for page in result.page_images:
            if provider == "anthropic":
                content_blocks.append(page.to_anthropic_format())
            else:
                # OpenAI and Gemini use similar format
                content_blocks.append(page.to_openai_format())

        return content_blocks


def create_pdf_converter(
    dpi: int = PDFConverter.DEFAULT_DPI,
    max_dimension: int = PDFConverter.MAX_PAGE_DIMENSION,
) -> PDFConverter:
    """
    Factory function to create a PDFConverter instance.

    Args:
        dpi: Resolution for rendering
        max_dimension: Maximum pixels per dimension

    Returns:
        Configured PDFConverter instance
    """
    return PDFConverter(dpi=dpi, max_dimension=max_dimension)
