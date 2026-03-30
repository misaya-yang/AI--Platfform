"""Document management service for knowledge base.

This service handles document CRUD operations, text extraction, and ingestion queuing.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from datetime import datetime
from typing import Any

import httpx

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import NotFoundError, PermissionDeniedError, ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage

logger = get_logger(__name__)

# Document type detection
DOCUMENT_MIME_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".html": "text/html",
    ".htm": "text/html",
}


def _get_extension(filename: str) -> str:
    """Get file extension from filename."""
    return filename.lower().split(".")[-1] if "." in filename else ""


def _detect_mime_type(filename: str) -> str:
    """Detect MIME type from filename."""
    ext = "." + _get_extension(filename)
    return DOCUMENT_MIME_TYPES.get(ext, "application/octet-stream")


from .common import ensure_dict as _ensure_dict  # noqa: E402


def _sanitize_filename(filename: str) -> str:
    """Sanitize filename for storage."""
    # Remove path components and unsafe characters
    filename = filename.split("/")[-1].split("\\")[-1]
    filename = re.sub(r"[^\w\s\-\.]", "_", filename)
    return filename[:255]  # Limit length


class DocumentService:
    """Service for managing knowledge base documents."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        dataset_service: Any | None = None,
    ):
        self.settings = settings
        self.db = database
        self.dataset_service = dataset_service

    # ========================================================================
    # Document CRUD Operations
    # ========================================================================

    async def create_document_from_text(
        self,
        user: UserContext,
        dataset_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a document from text content."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if not title.strip():
            raise ValidationFailedError("Document title is required")

        if not content.strip():
            raise ValidationFailedError("Document content is required")

        # Check dataset access
        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        document_id = str(uuid.uuid4())

        # Create document record
        await self.db.insert_document(
            document_id=document_id,
            dataset_id=dataset_id,
            title=title.strip(),
            content=content,
            doc_metadata=metadata or {},
            source_type="text",
        )

        # Queue for ingestion
        await self.enqueue_ingest(dataset_id, document_id)

        logger.info(f"Document created from text: {document_id}")

        return {
            "id": document_id,
            "title": title,
            "dataset_id": dataset_id,
            "status": "pending_ingestion",
        }

    async def create_document_from_upload(
        self,
        user: UserContext,
        dataset_id: str,
        filename: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a document from uploaded file."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if not filename:
            raise ValidationFailedError("Filename is required")

        if not content:
            raise ValidationFailedError("File content is required")

        # Check dataset access
        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        filename = _sanitize_filename(filename)
        mime_type = _detect_mime_type(filename)

        # Extract text based on file type
        extracted_text = ""
        try:
            extracted_text = await self._extract_text_from_bytes(content, mime_type)
        except OSError as e:
            logger.warning(f"File I/O error extracting text from {filename}: {e}")
            extracted_text = ""
        except ValueError as e:
            logger.warning(f"Invalid file format for {filename}: {e}")
            extracted_text = ""
        except RuntimeError as e:
            logger.warning(f"Processing error extracting text from {filename}: {e}")
            extracted_text = ""

        document_id = str(uuid.uuid4())

        # Create document record
        doc_metadata = _ensure_dict(metadata)
        doc_metadata.update(
            {
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(content),
                "extracted_text_length": len(extracted_text),
            }
        )

        await self.db.insert_document(
            document_id=document_id,
            dataset_id=dataset_id,
            title=filename,
            content=extracted_text,
            doc_metadata=doc_metadata,
            source_type="upload",
        )

        # Queue for ingestion
        await self.enqueue_ingest(dataset_id, document_id)

        logger.info(f"Document created from upload: {document_id} ({filename})")

        return {
            "id": document_id,
            "title": filename,
            "dataset_id": dataset_id,
            "status": "pending_ingestion",
        }

    async def create_document_from_url(
        self,
        user: UserContext,
        dataset_id: str,
        url: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a document from URL."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if not url.strip():
            raise ValidationFailedError("URL is required")

        # Check dataset access
        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        # Fetch content
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.text
        except httpx.HTTPError as e:
            raise ValidationFailedError(f"Failed to fetch URL: {e}")

        document_id = str(uuid.uuid4())

        # Create document record
        doc_metadata = _ensure_dict(metadata)
        doc_metadata.update(
            {
                "source_url": url,
                "fetched_at": datetime.utcnow().isoformat(),
            }
        )

        await self.db.insert_document(
            document_id=document_id,
            dataset_id=dataset_id,
            title=title or url,
            content=content,
            doc_metadata=doc_metadata,
            source_type="url",
        )

        # Queue for ingestion
        await self.enqueue_ingest(dataset_id, document_id)

        logger.info(f"Document created from URL: {document_id}")

        return {
            "id": document_id,
            "title": title or url,
            "dataset_id": dataset_id,
            "status": "pending_ingestion",
        }

    async def batch_create_documents(
        self,
        user: UserContext,
        dataset_id: str,
        documents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Batch create documents."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if not documents:
            raise ValidationFailedError("No documents provided")

        # Check dataset access
        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        created_ids = []
        errors = []

        for idx, doc in enumerate(documents):
            try:
                if "content" in doc:
                    result = await self.create_document_from_text(
                        user=user,
                        dataset_id=dataset_id,
                        title=doc.get("title", f"Document {idx + 1}"),
                        content=doc["content"],
                        metadata=doc.get("metadata"),
                    )
                elif "url" in doc:
                    result = await self.create_document_from_url(
                        user=user,
                        dataset_id=dataset_id,
                        url=doc["url"],
                        title=doc.get("title"),
                        metadata=doc.get("metadata"),
                    )
                else:
                    errors.append({"index": idx, "error": "No content or URL provided"})
                    continue

                created_ids.append(result["id"])
            except ValidationFailedError as e:
                errors.append({"index": idx, "error": f"Validation failed: {e}"})
            except PermissionDeniedError as e:
                errors.append({"index": idx, "error": f"Permission denied: {e}"})
            except NotFoundError as e:
                errors.append({"index": idx, "error": f"Not found: {e}"})
            except OSError as e:
                errors.append({"index": idx, "error": f"File error: {e}"})

        return {
            "created": len(created_ids),
            "document_ids": created_ids,
            "errors": errors,
        }

    async def list_documents(self, user: UserContext, dataset_id: str) -> list[dict[str, Any]]:
        """List all documents in a dataset."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "viewer")

        return await self.db.get_documents(dataset_id)

    async def get_document(
        self, user: UserContext, dataset_id: str, document_id: str
    ) -> dict[str, Any]:
        """Get document details."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "viewer")

        doc = await self.db.get_document(document_id)
        if not doc or doc.get("dataset_id") != dataset_id:
            raise NotFoundError(f"Document not found: {document_id}")
        return doc

    async def update_document(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Update document metadata."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        # Verify document exists
        await self.get_document(user, dataset_id, document_id)

        # Build update fields
        fields = {}
        if "title" in updates:
            fields["title"] = updates["title"].strip()
        if "metadata" in updates:
            fields["doc_metadata"] = _ensure_dict(updates["metadata"])

        if fields:
            await self.db.update_document(document_id, fields)
            logger.info(f"Document updated: {document_id}")

        return {"id": document_id, "updated": True}

    async def delete_document(self, user: UserContext, dataset_id: str, document_id: str) -> bool:
        """Delete a document."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        # Verify document exists
        await self.get_document(user, dataset_id, document_id)

        await self.db.delete_document(document_id)
        logger.info(f"Document deleted: {document_id}")
        return True

    async def batch_delete_documents(
        self,
        user: UserContext,
        dataset_id: str,
        document_ids: list[str],
    ) -> dict[str, Any]:
        """Batch delete documents."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if not document_ids:
            raise ValidationFailedError("No document IDs provided")

        # Check dataset access
        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        deleted = 0
        errors = []

        for doc_id in document_ids:
            try:
                await self.delete_document(user, dataset_id, doc_id)
                deleted += 1
            except Exception as e:
                errors.append({"document_id": doc_id, "error": str(e)})

        return {"deleted": deleted, "errors": errors}

    async def set_document_enabled(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Enable or disable a document."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        await self.get_document(user, dataset_id, document_id)
        await self.db.update_document(document_id, {"enabled": enabled})

        return {"id": document_id, "enabled": enabled}

    async def set_document_archived(
        self,
        user: UserContext,
        dataset_id: str,
        document_id: str,
        archived: bool,
    ) -> dict[str, Any]:
        """Archive or unarchive a document."""
        from .dataset_service import _require_not_guest

        _require_not_guest(user)

        if self.dataset_service:
            await self.dataset_service._require_dataset_permission(user, dataset_id, "editor")

        await self.get_document(user, dataset_id, document_id)
        await self.db.update_document(document_id, {"archived": archived})

        return {"id": document_id, "archived": archived}

    async def get_document_statistics(self, document_id: str) -> dict[str, Any]:
        """Get statistics for a document."""
        return await self.db.get_document_statistics(document_id)

    # ========================================================================
    # Ingestion
    # ========================================================================

    async def enqueue_ingest(self, dataset_id: str, document_id: str) -> None:
        """Queue document for ingestion processing."""
        await self.db.enqueue_ingest_task(dataset_id, document_id)

    async def recover_stuck_documents(self, max_age_minutes: int = 30) -> int:
        """Recover documents stuck in processing state."""
        return await self.db.recover_stuck_documents(max_age_minutes)

    # ========================================================================
    # Text Extraction
    # ========================================================================

    async def _extract_text_from_bytes(self, content: bytes, mime_type: str) -> str:
        """Extract text from file bytes based on MIME type."""
        if mime_type == "text/plain" or mime_type == "text/markdown":
            return self._decode_text_bytes(content)
        elif mime_type == "text/html":
            return self._extract_text_from_html(self._decode_text_bytes(content))
        elif mime_type == "application/pdf":
            return await self._extract_text_from_pdf(content)
        elif mime_type in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ):
            return self._extract_text_from_docx(content)
        else:
            # Try to decode as text
            return self._decode_text_bytes(content)

    async def _extract_text_from_pdf(self, content: bytes) -> str:
        """Extract text from PDF."""
        try:
            import pdfplumber

            with io.BytesIO(content) as pdf_file, pdfplumber.open(pdf_file) as pdf:
                text_parts = []
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
                return "\n\n".join(text_parts)
        except Exception as e:
            logger.warning(f"pdfplumber extraction failed: {e}")
            # Fallback to PyPDF2
            try:
                import pypdf

                with io.BytesIO(content) as pdf_file:
                    reader = pypdf.PdfReader(pdf_file)
                    text_parts = []
                    for page in reader.pages:
                        text_parts.append(page.extract_text() or "")
                    return "\n\n".join(text_parts)
            except Exception as e2:
                logger.warning(f"PyPDF2 extraction failed: {e2}")
                return ""

    def _extract_text_from_docx(self, content: bytes) -> str:
        """Extract text from DOCX."""
        try:
            import docx

            with io.BytesIO(content) as doc_file:
                doc = docx.Document(doc_file)
                text_parts = []
                for para in doc.paragraphs:
                    if para.text:
                        text_parts.append(para.text)
                return "\n".join(text_parts)
        except Exception as e:
            logger.warning(f"DOCX extraction failed: {e}")
            return ""

    def _extract_text_from_html(self, html: str) -> str:
        """Extract text from HTML."""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            # Fallback to regex
            text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", "", text)
            return text.strip()

    def _decode_text_bytes(self, content: bytes) -> str:
        """Decode bytes to string with encoding detection."""
        # Try UTF-8 first
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # Try common encodings
        for encoding in ["gbk", "gb2312", "big5", "latin-1", "cp1252"]:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue

        # Last resort: replace errors
        return content.decode("utf-8", errors="replace")
