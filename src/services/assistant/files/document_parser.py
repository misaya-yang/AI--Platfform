"""
Document Parser for File Upload Analysis.

Extracts text content from various document formats using the unstructured library.
Supports PDF, DOCX, DOC, TXT, MD, CSV, XLSX, and HTML files.
"""

from __future__ import annotations

import os
from pathlib import Path

from ....core.observability.logging import get_logger

try:
    from unstructured.partition.auto import partition as unstructured_partition
except ImportError:
    unstructured_partition = None

# Expose partition for patching in tests.
partition = unstructured_partition

logger = get_logger(__name__)

# Default storage path (matches files.py configuration)
FILE_STORAGE_PATH = Path(os.getenv("FILE_STORAGE_PATH", "./uploads")).expanduser()


class DocumentParseError(Exception):
    """Raised when document parsing fails."""

    def __init__(self, message: str, file_path: str, original_error: Exception | None = None):
        self.file_path = file_path
        self.original_error = original_error
        super().__init__(message)


class DocumentParser:
    """
    Document parser using the unstructured library.

    Extracts plain text content from various document formats for injection
    into model inputs.

    Example:
        parser = DocumentParser()
        text = await parser.parse("/uploads/user123/abc123_document.pdf")
    """

    # Supported file types with their MIME types
    SUPPORTED_TYPES: dict[str, str] = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".html": "text/html",
        ".htm": "text/html",
    }

    def __init__(self, storage_base_path: Path | None = None):
        """
        Initialize the document parser.

        Args:
            storage_base_path: Base path for file storage. If None, uses
                FILE_STORAGE_PATH environment variable or ./uploads default.
        """
        self.storage_base_path = storage_base_path or FILE_STORAGE_PATH

    def _resolve_path(self, file_path: str) -> Path:
        """
        Resolve API file path to actual disk path.

        API path format: /uploads/{user_id}/{file_id}_{timestamp}.{ext}
        Disk path format: {FILE_STORAGE_PATH}/uploads/{user_id}/{file_id}_{timestamp}.{ext}

        Args:
            file_path: File path from API (e.g., /uploads/admin/abc123_20240101_120000.pdf)

        Returns:
            Path: Resolved absolute path to the file on disk

        Raises:
            DocumentParseError: If the path format is invalid or file doesn't exist
        """
        # Normalize the path
        file_path = file_path.strip()

        # Handle paths that start with /uploads/
        if file_path.startswith("/uploads/"):
            # Remove leading slash and prepend storage base path
            relative_path = file_path[1:]  # Remove leading /
            actual_path = self.storage_base_path / relative_path
            needs_security_check = True
        elif file_path.startswith("uploads/"):
            # Path already relative without leading slash
            actual_path = self.storage_base_path / file_path
            needs_security_check = True
        else:
            # Assume it's already an absolute or relative path
            actual_path = Path(file_path)
            if actual_path.is_absolute():
                # Trust absolute paths (e.g., temp files from file_processor)
                # They have already been validated by the caller
                needs_security_check = False
            else:
                actual_path = self.storage_base_path / file_path
                needs_security_check = True

        # Normalize path without resolving symlinks (preserve /var on macOS)
        actual_path = Path(os.path.abspath(str(actual_path)))

        # Security check: ensure path is within storage base (only for relative paths)
        if needs_security_check:
            base_path = Path(os.path.abspath(str(self.storage_base_path)))
            try:
                actual_path.relative_to(base_path)
            except ValueError:
                logger.warning(f"[Security] Path traversal attempt detected: {file_path}")
                raise DocumentParseError(
                    "Invalid file path: path must be within storage directory",
                    file_path=file_path,
                )

        # Check file exists
        if not actual_path.exists():
            raise DocumentParseError(
                f"File not found: {file_path}",
                file_path=file_path,
            )

        if not actual_path.is_file():
            raise DocumentParseError(
                f"Path is not a file: {file_path}",
                file_path=file_path,
            )

        return actual_path

    def _validate_extension(self, file_path: Path) -> str:
        """
        Validate that the file extension is supported.

        Args:
            file_path: Path to the file

        Returns:
            str: The validated file extension (lowercase)

        Raises:
            DocumentParseError: If the file type is not supported
        """
        ext = file_path.suffix.lower()
        if ext not in self.SUPPORTED_TYPES:
            raise DocumentParseError(
                f"Unsupported file type: {ext}. Supported types: {', '.join(sorted(self.SUPPORTED_TYPES.keys()))}",
                file_path=str(file_path),
            )
        return ext

    async def parse(self, file_path: str) -> str:
        """
        Parse a document and extract its text content.

        This method handles path resolution from API paths to disk paths,
        validates the file type, and uses unstructured library to extract text.

        Args:
            file_path: File path from API (e.g., /uploads/user123/abc123.pdf)

        Returns:
            str: Extracted plain text content from the document

        Raises:
            DocumentParseError: If parsing fails due to invalid path, unsupported
                file type, or extraction error
        """
        # Resolve the actual file path
        actual_path = self._resolve_path(file_path)

        # Validate file extension
        ext = self._validate_extension(actual_path)

        logger.info(f"[DocumentParser] Parsing file: {file_path} -> {actual_path}, type={ext}")

        try:
            if partition is None:
                raise ImportError("unstructured is not installed")

            # Use unstructured to parse the document
            elements = partition(filename=str(actual_path))

            # Extract text from all elements
            text_parts = []
            for element in elements:
                if hasattr(element, "text") and element.text:
                    text_parts.append(element.text.strip())

            # Join all text parts with double newlines
            result = "\n\n".join(text_parts)

            logger.info(
                f"[DocumentParser] Successfully parsed {file_path}: "
                f"{len(elements)} elements, {len(result)} chars"
            )

            return result

        except ImportError as e:
            logger.error(f"[DocumentParser] unstructured library not installed: {e}")
            raise DocumentParseError(
                "Document parsing requires the 'unstructured' library. "
                "Install with: pip install unstructured[all-docs]",
                file_path=file_path,
                original_error=e,
            )

        except Exception as e:
            logger.error(
                f"[DocumentParser] Failed to parse {file_path}: {e}",
                exc_info=True,
            )
            raise DocumentParseError(
                f"Failed to parse document: {str(e)}",
                file_path=file_path,
                original_error=e,
            )

    async def parse_to_text(self, file_path: str) -> str:
        """
        Alias for parse() method for clarity.

        Args:
            file_path: File path from API

        Returns:
            str: Extracted plain text content
        """
        return await self.parse(file_path)

    def is_supported(self, file_path: str) -> bool:
        """
        Check if a file type is supported for parsing.

        Args:
            file_path: File path to check

        Returns:
            bool: True if the file type is supported, False otherwise
        """
        ext = Path(file_path).suffix.lower()
        return ext in self.SUPPORTED_TYPES

    def get_mime_type(self, file_path: str) -> str | None:
        """
        Get the MIME type for a file based on its extension.

        Args:
            file_path: File path to check

        Returns:
            Optional[str]: MIME type if supported, None otherwise
        """
        ext = Path(file_path).suffix.lower()
        return self.SUPPORTED_TYPES.get(ext)


# Convenience function for one-off parsing
async def parse_document(file_path: str, storage_base_path: Path | None = None) -> str:
    """
    Parse a document and return its text content.

    This is a convenience function for one-off document parsing.

    Args:
        file_path: File path from API (e.g., /uploads/user123/abc123.pdf)
        storage_base_path: Optional base path for file storage

    Returns:
        str: Extracted plain text content

    Raises:
        DocumentParseError: If parsing fails
    """
    parser = DocumentParser(storage_base_path=storage_base_path)
    return await parser.parse(file_path)
