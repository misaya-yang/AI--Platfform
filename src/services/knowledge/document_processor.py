"""Document text extraction for various file formats (PDF, DOCX, DOC, HTML, etc.)."""
from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import re
import tempfile
from typing import Any

from ai_gateway_core.exceptions import ValidationFailedError
from ai_gateway_core.logging import get_logger
from .utils import normalize_text

logger = get_logger(__name__)

# VLM rate-limiting state (shared across instances)
_global_vlm_semaphore: asyncio.Semaphore | None = None
_global_vlm_max_concurrent: int = 10
_global_vlm_lock = asyncio.Lock()


class DocumentProcessor:
    """Extracts and normalises text from various document formats."""

    def __init__(self, settings: Any, vlm_service: Any | None = None):
        self.settings = settings
        self.vlm_service = vlm_service

    # ------------------------------------------------------------------
    # VLM callback
    # ------------------------------------------------------------------

    def create_vlm_callback(self):
        """Create a VLM callback for document processing.

        Uses a global semaphore to limit concurrent VLM API calls across all
        document processing tasks. This prevents overwhelming the API when
        multiple documents are processed simultaneously.
        """
        global _global_vlm_semaphore, _global_vlm_max_concurrent

        vlm = self.vlm_service
        if vlm is None:
            return None

        # Initialize global semaphore if not done yet
        vlm_max_concurrent = self.settings.knowledge.vlm_max_concurrent
        if _global_vlm_semaphore is None or _global_vlm_max_concurrent != vlm_max_concurrent:
            _global_vlm_max_concurrent = vlm_max_concurrent
            _global_vlm_semaphore = asyncio.Semaphore(vlm_max_concurrent)
            logger.info(
                f"Initialized global VLM semaphore with max_concurrent={vlm_max_concurrent}"
            )

        semaphore = _global_vlm_semaphore

        async def _vlm_extract_text(image_bytes: bytes, lang: str) -> str:
            prompt = (
                "Extract ALL text from this document page exactly as written. "
                "Preserve the original structure, paragraphs, and formatting. "
                "Do not summarize or interpret — output only the raw text content."
            )
            if lang == "ar":
                prompt = (
                    "استخرج جميع النصوص من هذه الصفحة كما هي مكتوبة بالضبط. "
                    "حافظ على الهيكل الأصلي والفقرات والتنسيق. "
                    "لا تلخص أو تفسر — أخرج فقط المحتوى النصي الخام."
                )
            try:
                # Use global semaphore to limit concurrent VLM calls
                async with semaphore:
                    result = await vlm.describe_image(
                        image_bytes=image_bytes,
                        prompt=prompt,
                        image_type="document",
                        max_tokens=2000,
                    )
                    return result.description
            except Exception as e:
                logger.warning(f"VLM text extraction failed: {e}")
                return ""

        return _vlm_extract_text

    # ------------------------------------------------------------------
    # Sanitisation helpers
    # ------------------------------------------------------------------

    def _sanitize_text_for_db(self, text: str) -> str:
        """Remove NULL bytes and other characters that PostgreSQL cannot handle."""
        if not text:
            return ""
        # Remove NULL bytes (0x00) which PostgreSQL rejects
        text = text.replace("\x00", "")
        # Remove other control characters except common whitespace
        cleaned = []
        for char in text:
            # Keep printable chars, newlines, tabs, carriage returns
            if char.isprintable() or char in "\n\r\t" or ord(char) > 31:
                cleaned.append(char)
        return "".join(cleaned)

    def decode_text_bytes(self, content: bytes) -> str:
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"):
            try:
                decoded = content.decode(enc)
                # Sanitize for PostgreSQL compatibility
                return self._sanitize_text_for_db(decoded)
            except Exception:
                continue
        raise ValidationFailedError("Unable to decode uploaded file as text")

    # ------------------------------------------------------------------
    # PDF extraction
    # ------------------------------------------------------------------

    def clean_pdf_content(self, text: str) -> str:
        """Clean PDF extracted content, removing TOC lines and noise."""
        if not text:
            return ""

        lines = text.split("\n")
        cleaned_lines = []

        # Track if we're in TOC section
        toc_indicators = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Skip lines that look like TOC entries - VERY aggressive patterns
            # Pattern: any combination of dots followed by page numbers
            if re.search(r"\.{2,}\s*\d+\s*$", line):  # ....2
                toc_indicators += 1
                continue
            if re.search(r"(\.\s+){2,}\d+\s*$", line):  # . . . 2
                toc_indicators += 1
                continue
            if re.search(r"·{2,}\s*\d+\s*$", line):  # ···2
                toc_indicators += 1
                continue
            if re.search(r"…+\s*\d+\s*$", line):  # …2
                toc_indicators += 1
                continue

            # Skip lines starting with dots (like "......2")
            if re.match(r"^[\.·…\s]+\d+", line):
                toc_indicators += 1
                continue

            # Skip lines that contain excessive dots anywhere
            dot_count = len(re.findall(r"[\.·…]", line))
            if len(line) > 5 and dot_count > 3:
                # More than 3 dots in a short line, likely TOC
                if dot_count / len(line) > 0.15:
                    toc_indicators += 1
                    continue

            # Skip very short lines that are just page numbers or section numbers
            if re.match(r"^[\d\.\s]+$", line) and len(line) < 10:
                continue

            # Skip lines that look like "2.1 2.1标题..."
            if re.match(r"^\d+(\.\d+)*\s+\d+(\.\d+)*", line):
                continue

            # Clean up remaining dots sequences in the line
            line = re.sub(r"\.{3,}", " ", line)
            line = re.sub(r"(\.\s+){2,}", " ", line)
            line = re.sub(r"·{2,}", " ", line)
            line = re.sub(r"…{1,}", " ", line)

            # Clean up repeated spaces
            line = re.sub(r"\s{2,}", " ", line)

            line = line.strip()
            if line and len(line) > 2:  # Skip very short remnants
                cleaned_lines.append(line)

        result = "\n".join(cleaned_lines)

        # If a large portion of content was TOC-like, we may have a TOC-heavy doc
        # Log this for debugging
        if toc_indicators > 10:
            logger.info(f"Cleaned {toc_indicators} TOC-like lines from PDF")

        return result

    def extract_text_from_pdf_bytes(self, content: bytes) -> str:
        """Extract text from PDF with table-to-markdown conversion.

        Uses pdfplumber if available for better table extraction,
        falls back to pypdf for basic text extraction.
        Scanned PDFs are handled via multimodal image embedding (no OCR).
        """
        import traceback
        from io import BytesIO

        text = ""

        # Try pdfplumber first (better table extraction)
        try:
            # Explicit import check
            import pdfplumber

            text = self.extract_pdf_with_pdfplumber(BytesIO(content))
        except ImportError as e:
            logger.warning(f"pdfplumber import failed: {e}")
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed: {e}")
            traceback.print_exc()
            # Fall back to pypdf if pdfplumber fails

        # Fallback to pypdf if pdfplumber didn't work
        if not text:
            try:
                from pypdf import PdfReader  # type: ignore

                reader = PdfReader(BytesIO(content))
                parts: list[str] = []
                for i, page in enumerate(reader.pages):
                    try:
                        t = page.extract_text() or ""
                    except Exception as e:
                        logger.warning(f"Failed to extract text from page {i}: {e}")
                        t = ""
                    if t:
                        parts.append(t)
                text = "\n".join(parts)
            except ImportError as exc:
                logger.error(f"pypdf import failed: {exc}")
                raise ValidationFailedError(
                    "PDF parsing requires pypdf (pip install pypdf) or pdfplumber"
                ) from exc
            except Exception as exc:
                logger.error(f"pypdf parsing failed: {exc}")
                traceback.print_exc()

        # OCR fallback for scanned/low-text PDFs
        min_chars = getattr(self.settings.knowledge, "pdf_min_text_chars_for_ocr", 200)
        ocr_enabled = getattr(self.settings.knowledge, "ocr_enabled", True)
        if ocr_enabled and (not text or len(text.strip()) < min_chars):
            ocr_text = self.ocr_pdf_bytes(content)
            if ocr_text and len(ocr_text.strip()) >= min_chars:
                text = ocr_text

        if not text or not text.strip():
            raise ValidationFailedError("Failed to extract any text from PDF")

        text = self._sanitize_text_for_db(normalize_text(text))
        return self.clean_pdf_content(text)

    def ocr_pdf_bytes(self, content: bytes) -> str:
        """OCR a PDF using PyMuPDF rendering + Tesseract CLI.

        Uses shared OCR utilities from ocr_utils module.
        """
        from .ocr_utils import OCRCConfig
        from .ocr_utils import ocr_pdf_bytes as _ocr_pdf

        config = OCRCConfig.from_settings(self.settings.knowledge)
        max_workers = int(getattr(self.settings.knowledge, "ocr_page_concurrency", 2) or 1)

        return _ocr_pdf(content, config=config, max_workers=max_workers)

    def extract_pdf_with_pdfplumber(self, pdf_stream) -> str:
        """Extract PDF content using pdfplumber with table detection."""
        import pdfplumber  # type: ignore

        parts: list[str] = []

        with pdfplumber.open(pdf_stream) as pdf:
            for _page_num, page in enumerate(pdf.pages):
                page_parts: list[str] = []

                # Extract tables first
                tables = page.extract_tables() or []

                for table in tables:
                    if table and len(table) > 0:
                        md_table = self.pdf_table_to_markdown(table)
                        if md_table:
                            page_parts.append("\n" + md_table + "\n")

                # Extract text (excluding table areas if possible)
                text = page.extract_text() or ""
                if text.strip():
                    # If we have tables, the text might include table content
                    # Still add it but tables are now properly formatted
                    page_parts.insert(0, text)

                if page_parts:
                    parts.append("\n".join(page_parts))

        text = "\n\n".join(parts)
        return self._sanitize_text_for_db(normalize_text(text))

    def pdf_table_to_markdown(self, table: list[list]) -> str:
        """Convert a PDF table (list of rows) to Markdown format."""
        if not table or len(table) == 0:
            return ""

        # Filter out empty rows
        table = [row for row in table if row and any(cell for cell in row)]
        if not table:
            return ""

        # Get max columns
        total_cols = max(len(row) for row in table)
        if total_cols == 0:
            return ""

        md_lines: list[str] = []

        for i, row in enumerate(table):
            # Pad row to total_cols
            cells = list(row) + [""] * (total_cols - len(row))
            # Clean cell content
            cells = [
                str(cell or "").strip().replace("|", "\\|").replace("\n", " ") for cell in cells
            ]
            md_lines.append("| " + " | ".join(cells) + " |")

            # Add separator after header
            if i == 0:
                md_lines.append("| " + " | ".join(["---"] * total_cols) + " |")

        return "\n".join(md_lines)

    # ------------------------------------------------------------------
    # DOCX / DOC extraction
    # ------------------------------------------------------------------

    def extract_text_from_docx_bytes(self, content: bytes) -> str:
        """Extract text from DOCX with table-to-markdown conversion."""
        try:
            from docx import Document  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "DOCX parsing requires python-docx (pip install python-docx)"
            ) from exc

        try:
            from io import BytesIO

            doc = Document(BytesIO(content))
            parts: list[str] = []

            # Get all paragraphs and tables in document order
            paragraphs = list(getattr(doc, "paragraphs", []) or [])
            tables = list(getattr(doc, "tables", []) or [])

            para_idx = 0
            table_idx = 0

            # Process document body in order (paragraphs and tables interleaved)
            for element in doc.element.body:
                tag = getattr(element, "tag", None)
                if tag is None:
                    continue
                tag_str = str(tag)

                if tag_str.endswith("}p"):  # Paragraph
                    if para_idx < len(paragraphs):
                        para = paragraphs[para_idx]
                        t = (para.text or "").strip()
                        if t:
                            parts.append(t)
                        para_idx += 1

                elif tag_str.endswith("}tbl"):  # Table
                    if table_idx < len(tables):
                        table = tables[table_idx]
                        md_table = self.table_to_markdown(table)
                        if md_table:
                            parts.append("\n" + md_table + "\n")
                        table_idx += 1

            text = "\n".join(parts)
            text = normalize_text(text)
            if not text:
                raise ValidationFailedError("DOCX parsed but no text extracted")
            return self._sanitize_text_for_db(text)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse DOCX: {exc}") from exc

    def table_to_markdown(self, table) -> str:
        """Convert a python-docx table to Markdown format."""
        try:
            rows = list(getattr(table, "rows", []) or [])
            if not rows:
                return ""

            # Calculate total columns (handle merged cells)
            total_cols = (
                max(len(list(getattr(row, "cells", []) or [])) for row in rows) if rows else 0
            )
            if total_cols == 0:
                return ""

            md_lines: list[str] = []

            # Header row
            header_row = rows[0]
            headers = self.parse_table_row(header_row, total_cols)
            md_lines.append("| " + " | ".join(headers) + " |")
            md_lines.append("| " + " | ".join(["---"] * total_cols) + " |")

            # Data rows
            for row in rows[1:]:
                cells = self.parse_table_row(row, total_cols)
                md_lines.append("| " + " | ".join(cells) + " |")

            return "\n".join(md_lines)
        except Exception:
            return ""

    def parse_table_row(self, row, total_cols: int) -> list[str]:
        """Parse a table row into a list of cell texts."""
        cells = list(getattr(row, "cells", []) or [])
        row_cells = [""] * total_cols
        col_idx = 0

        for cell in cells:
            if col_idx >= total_cols:
                break
            # Skip already filled cells (from previous merged cells)
            while col_idx < total_cols and row_cells[col_idx]:
                col_idx += 1
            if col_idx >= total_cols:
                break

            # Get cell text
            cell_text = str(getattr(cell, "text", "") or "").strip()
            # Clean up cell text for markdown (escape pipes, remove newlines)
            cell_text = cell_text.replace("|", "\\|").replace("\n", " ")

            # Handle grid span (column merging)
            grid_span = getattr(cell, "grid_span", 1) or 1
            for i in range(grid_span):
                if col_idx + i < total_cols:
                    row_cells[col_idx + i] = cell_text if i == 0 else ""
            col_idx += grid_span

        return row_cells

    def extract_text_from_doc_bytes(self, content: bytes) -> str:
        try:
            import textract  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "DOC parsing requires textract (pip install textract) and system extractors."
            ) from exc

        try:
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=True) as tmp:
                tmp.write(content)
                tmp.flush()
                raw = textract.process(tmp.name)

            decoded = raw.decode("utf-8", errors="ignore")
            text = normalize_text(decoded)
            if not text:
                raise ValidationFailedError("DOC parsed but no text extracted")
            return self._sanitize_text_for_db(text)
        except ValidationFailedError:
            raise
        except Exception as exc:
            raise ValidationFailedError(f"Failed to parse DOC: {exc}") from exc

    # ------------------------------------------------------------------
    # HTML extraction
    # ------------------------------------------------------------------

    def extract_text_from_html(self, html: str) -> str:
        """Extract text from HTML with improved handling of various content types."""
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except Exception as exc:
            raise ValidationFailedError(
                "HTML parsing requires beautifulsoup4 (pip install beautifulsoup4 lxml)"
            ) from exc

        soup = BeautifulSoup(html or "", "lxml")

        # Remove non-content elements
        for tag in soup(
            ["script", "style", "noscript", "header", "footer", "nav", "aside", "iframe", "form"]
        ):
            with contextlib.suppress(Exception):
                tag.decompose()

        # Try to find main content area
        main_content = None
        for selector in [
            "main",
            "article",
            "[role='main']",
            ".content",
            "#content",
            ".post",
            ".article",
        ]:
            try:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            except Exception:
                continue

        # Use main content if found, otherwise use full body
        content_root = main_content or soup.body or soup

        parts: list[str] = []

        # Extract title
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            parts.append(f"# {title_tag.string.strip()}")

        # Extract headings and paragraphs
        for element in content_root.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote", "pre", "code"]
        ):
            text = element.get_text(separator=" ", strip=True)
            if text:
                tag_name = element.name
                if tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
                    level = int(tag_name[1])
                    parts.append("#" * level + " " + text)
                elif tag_name == "li":
                    parts.append("• " + text)
                elif tag_name in ["td", "th"]:
                    continue  # Handle tables separately
                else:
                    parts.append(text)

        # Handle tables
        for table in content_root.find_all("table"):
            table_rows: list[str] = []
            for tr in table.find_all("tr"):
                cells = [
                    cell.get_text(separator=" ", strip=True) for cell in tr.find_all(["th", "td"])
                ]
                if any(cells):
                    table_rows.append(" | ".join(c if c else "-" for c in cells))
            if table_rows:
                parts.append("\n".join(table_rows))

        # Fallback: if no structured content found, use simple text extraction
        if not parts:
            text = content_root.get_text(separator="\n", strip=True)
            lines = [ln.strip() for ln in (text or "").splitlines()]
            lines = [ln for ln in lines if ln]
            return self._sanitize_text_for_db(normalize_text("\n".join(lines)))

        result = "\n\n".join(parts)
        result = normalize_text(result)
        return self._sanitize_text_for_db(result)

    # ------------------------------------------------------------------
    # Unified dispatcher
    # ------------------------------------------------------------------

    def extract_text_from_bytes(
        self,
        content: bytes,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> tuple[str, str]:
        name = (filename or "").strip().lower()
        mime = (mime_type or "").strip().lower()

        # Legacy Office (.doc) is OLE2 Compound Document.
        if (
            content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            or name.endswith(".doc")
            or "application/msword" in mime
        ):
            return self.extract_text_from_doc_bytes(content), "application/msword"

        # PDF
        if content.startswith(b"%PDF") or name.endswith(".pdf") or "application/pdf" in mime:
            return self.extract_text_from_pdf_bytes(content), "application/pdf"

        # DOCX (OOXML zip)
        if (
            content.startswith(b"PK\x03\x04")
            or name.endswith(".docx")
            or "wordprocessingml.document" in mime
        ):
            return (
                self.extract_text_from_docx_bytes(content),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

        # HTML (primarily for URL ingestion)
        if "text/html" in mime or name.endswith(".html") or name.endswith(".htm"):
            decoded = self.decode_text_bytes(content)
            return self.extract_text_from_html(decoded), "text/html"

        # Markdown
        if name.endswith(".md") or mime in {"text/markdown", "text/x-markdown"}:
            return self.decode_text_bytes(content), "text/markdown"

        # Plain text fallback
        decoded = self.decode_text_bytes(content)
        return decoded, (mime_type or "text/plain")
