"""
File Processor for Upload Analysis.

Processes uploaded files and converts them to model-consumable formats:
- Images: Convert to base64 for vision models, or generate descriptions for text models
- Documents: Extract text content, create session KB for long documents
- PDFs: Convert to images for vision models (preserves tables, embedded images)

This is a core component of the file upload analysis feature, integrating:
- PDFConverter for PDF-to-image conversion (vision models)
- DocumentParser for document parsing (text extraction fallback)
- VLM service for image descriptions
- KnowledgeClientLike for session-level temporary KB (long documents)

Supports remote storage backends (S3/OSS) via FileStorageLike:
- Files are downloaded from remote storage when needed
- Cached locally in temp directory during processing
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_gateway_core.auth import UserContext
from ai_gateway_core.logging import get_logger, record_internal_exception

from .document_parser import DocumentParseError, DocumentParser
from .file_strategy import (
    FileProcessingStrategyFactory,
    ProcessedContent,
    ProcessingStrategy,
)
from .pdf_converter import PDFConversionError, create_pdf_converter

if TYPE_CHECKING:
    from ai_gateway_core.knowledge import KnowledgeClientLike
    from ai_gateway_core.storage import FileStorageLike

# Type alias for progress callback
ProgressCallback = Callable[[str, int, int, str], Awaitable[None]]

logger = get_logger(__name__)

# Default storage path (matches files.py configuration)
FILE_STORAGE_PATH = Path(os.getenv("FILE_STORAGE_PATH", "./uploads")).expanduser()

# Supported image extensions
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

# Supported document extensions (from DocumentParser)
DOCUMENT_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".html", ".htm"}
)


class FileProcessError(Exception):
    """Raised when file processing fails."""

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
class ImageContent:
    """Base64-encoded image content for vision models."""

    base64_data: str
    media_type: str  # e.g., "image/png", "image/jpeg"
    file_path: str
    size_bytes: int

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to OpenAI vision API format."""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type};base64,{self.base64_data}",
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
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
class PDFPageContent:
    """PDF page converted to image for vision models."""

    page_number: int
    base64_data: str
    media_type: str  # Always "image/png"
    width: int
    height: int
    size_bytes: int
    file_path: str

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to OpenAI vision API format."""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type};base64,{self.base64_data}",
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
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
class DocumentStructure:
    """Structure analysis result for a document."""

    total_chars: int = 0
    total_lines: int = 0
    sections: list[dict[str, Any]] = field(default_factory=list)  # [{level, title, line}]
    has_headers: bool = False
    has_lists: bool = False
    has_tables: bool = False
    has_code_blocks: bool = False
    estimated_reading_time_min: int = 0
    key_topics: list[str] = field(default_factory=list)  # Extracted from headers


@dataclass
class ProcessedFiles:
    """Result of file processing for model consumption.

    Attributes:
        images: List of base64-encoded images for vision models
        pdf_pages: List of PDF pages converted to images for vision models
        text_content: Extracted text from short documents (< max_text_chars)
        image_descriptions: VLM-generated descriptions for text-only models
        session_kb_id: ID for session-level temporary KB (long documents)
        file_metadata: Metadata about each processed file
        requires_rag: Flag indicating long documents need RAG retrieval
        document_structure: Structure analysis for deep document understanding
    """

    images: list[ImageContent] = field(default_factory=list)
    pdf_pages: list[PDFPageContent] = field(default_factory=list)
    text_content: str = ""
    image_descriptions: list[str] = field(default_factory=list)
    session_kb_id: str | None = None
    file_metadata: list[dict[str, Any]] = field(default_factory=list)
    requires_rag: bool = False
    document_structure: DocumentStructure | None = None

    @property
    def has_images(self) -> bool:
        """Check if there are any images (including PDF pages)."""
        return len(self.images) > 0 or len(self.pdf_pages) > 0

    @property
    def has_pdf_pages(self) -> bool:
        """Check if there are any PDF page images."""
        return len(self.pdf_pages) > 0

    @property
    def has_text(self) -> bool:
        """Check if there is any text content."""
        return bool(self.text_content.strip())

    @property
    def has_descriptions(self) -> bool:
        """Check if there are any image descriptions."""
        return len(self.image_descriptions) > 0


class FileProcessor:
    """
    File processor for upload analysis.

    Processes uploaded files and converts them to formats suitable for
    different model capabilities:

    - Vision models: Images as base64, short docs as text
    - Text models: Image descriptions via VLM, short docs as text
    - Long documents: Mark for RAG retrieval (session KB)

    Example:
        processor = FileProcessor(vlm_service=vlm)
        result = await processor.process_files(
            file_paths=["/uploads/user123/abc123_doc.pdf", "/uploads/user123/def456_image.png"],
            session_id="session_123",
            user=user_context,
            model_supports_vision=True,
        )

        # For vision models
        for img in result.images:
            content.append(img.to_openai_format())

        # Text content from short documents
        if result.text_content:
            prompt += f"\\n\\nDocument content:\\n{result.text_content}"
    """

    # VLM prompt for generating image descriptions (for text-only models)
    IMAGE_DESCRIPTION_PROMPT = """请详细描述这张图片的内容。如果图片包含文字、数据、表格或图表，请提取并列出这些信息。
描述应该足够详细，让没有看到图片的人也能理解图片的内容。"""

    IMAGE_DESCRIPTION_PROMPT_EN = """Please describe this image in detail. If the image contains text, data, tables, or charts, please extract and list this information.
The description should be detailed enough for someone who hasn't seen the image to understand its content."""

    # Maximum pages to convert from PDF (to avoid huge context)
    MAX_PDF_PAGES = 20

    def __init__(
        self,
        vlm_service: Any | None = None,
        knowledge_service: KnowledgeClientLike | None = None,
        storage_base_path: Path | None = None,
        use_english_prompt: bool = False,
        max_pdf_pages: int = MAX_PDF_PAGES,
        file_storage: FileStorageLike | None = None,
        redis_client: Any | None = None,
    ):
        """
        Initialize FileProcessor.

        Args:
            vlm_service: VLM service for generating image descriptions.
                If None, image descriptions will not be generated for text models.
            knowledge_service: Knowledge service for creating session KB.
                If None, long documents will be marked for RAG but KB won't be created.
            storage_base_path: Base path for file storage. Defaults to FILE_STORAGE_PATH.
            use_english_prompt: Use English prompts for VLM (default: Chinese).
            max_pdf_pages: Maximum PDF pages to convert (default 20).
            file_storage: FileStorageLike for remote storage (S3/OSS).
                If provided, files will be downloaded from remote storage when not found locally.
            redis_client: Redis client for caching results.
        """
        self.vlm_service = vlm_service
        self.knowledge_service = knowledge_service
        self.storage_base_path = storage_base_path or FILE_STORAGE_PATH
        self.document_parser = DocumentParser(storage_base_path=self.storage_base_path)
        self.pdf_converter = create_pdf_converter(dpi=150)
        self.max_pdf_pages = max_pdf_pages
        self.file_storage = file_storage
        self.redis = redis_client
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self.description_prompt = (
            self.IMAGE_DESCRIPTION_PROMPT_EN
            if use_english_prompt
            else self.IMAGE_DESCRIPTION_PROMPT
        )

    def _get_cache_key(self, api_path: str, model_supports_vision: bool) -> str:
        """Generate cache key for file processing result."""
        # Simple cache key based on file path and processing mode
        # In production, this should include file hash or mtime
        mode = "vision" if model_supports_vision else "text"
        return f"file_proc:{api_path}:{mode}"

    async def _get_cached_result(self, cache_key: str) -> dict[str, Any] | None:
        """Get processed result from Redis cache."""
        if not self.redis:
            return None

        try:
            data = await self.redis.get(cache_key)
            if data:
                # RedisStorage returns parsed JSON (dict), native client returns bytes/str
                if isinstance(data, dict):
                    return data
                if isinstance(data, (str, bytes)):
                    return json.loads(data)
                return data
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )
        return None

    async def _cache_result(self, cache_key: str, result: dict[str, Any], ttl: int = 86400):
        """Cache processed result in Redis."""
        if not self.redis:
            return

        try:
            json_data = json.dumps(result)
            if hasattr(self.redis, "save"):
                # RedisStorage wrapper
                await self.redis.save(cache_key, json_data, ttl)
            else:
                # Native redis client
                await self.redis.setex(cache_key, ttl, json_data)
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )

    async def preprocess_file(
        self,
        file_path: str,
        user: UserContext,
        model_supports_vision: bool = True,  # Default to True to pre-generate images
    ) -> None:
        """
        Preprocess a file asynchronously and cache the result.

        This is designed to be called by a background task immediately after upload.
        """
        _ = user
        cache_key = self._get_cache_key(file_path, model_supports_vision)

        # Check cache first
        if await self._get_cached_result(cache_key):
            logger.info(f"[FileProcessor] File already cached: {file_path}")
            return

        logger.info(f"[FileProcessor] Preprocessing file: {file_path}")

        # We need a dummy ProcessedFiles object to accumulate results
        # This duplicates some logic from process_files but focused on single file
        # Ideally process_files calls this, but for now we implement a focused version

        try:
            # Resolve path (supports remote storage download)
            actual_path = await self._resolve_path_async(file_path)
            file_type = self._detect_file_type(actual_path)
            is_pdf = actual_path.suffix.lower() == ".pdf"

            processed_data = {
                "file_path": file_path,
                "images": [],
                "pdf_pages": [],
                "text_content": "",
                "image_descriptions": [],
                "metadata": {},
                "requires_rag": False,
            }

            if file_type == "image":
                image, description, metadata = await self._process_image(
                    file_path=actual_path,
                    api_path=file_path,
                    model_supports_vision=model_supports_vision,
                )
                if image:
                    # Convert ImageContent to dict for serialization
                    processed_data["images"].append(
                        {
                            "base64_data": image.base64_data,
                            "media_type": image.media_type,
                            "file_path": image.file_path,
                            "size_bytes": image.size_bytes,
                        }
                    )
                if description:
                    processed_data["image_descriptions"].append(description)
                processed_data["metadata"] = metadata

            elif is_pdf and model_supports_vision:
                pdf_pages, metadata = await self._process_pdf_as_images(
                    file_path=actual_path,
                    api_path=file_path,
                )
                if not pdf_pages:
                    fallback_text, needs_rag, fallback_meta = await self._fallback_pdf_to_text(
                        file_path=actual_path,
                        api_path=file_path,
                        max_text_chars=32000,
                    )
                    if fallback_text:
                        processed_data["text_content"] = fallback_text
                    if needs_rag:
                        processed_data["requires_rag"] = True
                    metadata.update(
                        {
                            "fallback_mode": "text",
                            "fallback_text_length": len(fallback_text),
                            "fallback_requires_rag": needs_rag,
                            "fallback_meta": fallback_meta,
                        }
                    )
                # Serialize PDF pages
                processed_data["pdf_pages"] = [
                    {
                        "page_number": p.page_number,
                        "base64_data": p.base64_data,
                        "media_type": p.media_type,
                        "width": p.width,
                        "height": p.height,
                        "size_bytes": p.size_bytes,
                        "file_path": p.file_path,
                    }
                    for p in pdf_pages
                ]
                processed_data["metadata"] = metadata

            elif file_type == "document":
                text, needs_rag, metadata = await self._process_document(
                    file_path=actual_path,
                    api_path=file_path,
                    max_text_chars=32000,
                )
                processed_data["text_content"] = text
                processed_data["requires_rag"] = needs_rag
                processed_data["metadata"] = metadata

            # Cache the result
            await self._cache_result(cache_key, processed_data)
            logger.info(f"[FileProcessor] Finished preprocessing: {file_path}")

        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )

    def cleanup(self) -> None:
        """Clean up temporary resources."""
        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.files.file_processor.internal_failure", e
                )
            finally:
                self._temp_dir = None

    def __del__(self) -> None:
        """Destructor to ensure cleanup."""
        self.cleanup()

    async def __aenter__(self) -> FileProcessor:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - cleanup temp resources."""
        self.cleanup()

    def _get_storage_key(self, file_path: str) -> str:
        """
        Convert API path to storage key.

        API path format: /uploads/{user_id}/{file_id}_{timestamp}.{ext}
        Storage key format: uploads/{user_id}/{file_id}_{timestamp}.{ext}
        """
        file_path = file_path.strip()
        if file_path.startswith("/"):
            return file_path[1:]  # Remove leading /
        return file_path

    async def _download_from_remote(self, file_path: str) -> Path | None:
        """
        Download file from remote storage to a temporary local path.

        Args:
            file_path: API file path (e.g., /uploads/user/file.pdf)

        Returns:
            Path to downloaded file, or None if download failed
        """
        if not self.file_storage:
            logger.warning("[FileProcessor] Cannot download: file_storage is None")
            return None

        storage_key = self._get_storage_key(file_path)
        logger.info(
            f"[FileProcessor] Attempting remote download: "
            f"api_path={file_path}, storage_key={storage_key}, "
            f"backend={self.file_storage.config.backend.value}"
        )

        try:
            # Download file content
            content = await self.file_storage.download_file(storage_key)

            if not content:
                logger.warning(f"[FileProcessor] Download returned empty content: {storage_key}")
                return None

            logger.info(f"[FileProcessor] Downloaded {len(content)} bytes from remote storage")

            # Create temp directory if needed
            if self._temp_dir is None:
                self._temp_dir = tempfile.TemporaryDirectory(prefix="file_processor_")

            # Create temp file with original extension
            filename = Path(file_path).name
            temp_path = Path(self._temp_dir.name) / filename

            # Write content to temp file
            temp_path.write_bytes(content)

            logger.info(
                f"[FileProcessor] Downloaded from remote storage: {storage_key} -> {temp_path}"
            )
            return temp_path

        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )
            return None

    async def _resolve_path_async(self, file_path: str) -> Path:
        """
        Resolve API file path to actual disk path, downloading from remote if needed.

        This is the async version that supports remote storage backends.

        Args:
            file_path: File path from API

        Returns:
            Path: Resolved absolute path to the file on disk

        Raises:
            FileProcessError: If the path is invalid or file doesn't exist
        """
        file_path = file_path.strip()

        # Reject obviously dangerous inputs early
        if not file_path:
            raise FileProcessError("Empty file path", file_path=file_path)
        if "\x00" in file_path:
            raise FileProcessError("Invalid file path: null byte", file_path=file_path)

        # Handle paths that start with /uploads/
        if file_path.startswith("/uploads/"):
            relative_path = file_path[1:]  # Remove leading /
            actual_path = self.storage_base_path / relative_path
        elif file_path.startswith("uploads/"):
            actual_path = self.storage_base_path / file_path
        else:
            candidate = Path(file_path)
            if candidate.is_absolute():
                # Reject absolute paths from untrusted input — they bypass
                # storage_base_path confinement and can read host files.
                raise FileProcessError(
                    "Absolute file paths are not permitted",
                    file_path=file_path,
                )
            actual_path = self.storage_base_path / file_path

        # Canonicalize, then verify containment within storage_base_path.
        # Without this check, ".." components can escape storage_base_path
        # (CVE-class path traversal).
        try:
            actual_path = actual_path.resolve()
            base_resolved = self.storage_base_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise FileProcessError(
                f"Failed to resolve file path: {exc}",
                file_path=file_path,
            ) from exc

        try:
            actual_path.relative_to(base_resolved)
        except ValueError:
            logger.warning(f"[Security] Path traversal attempt: {file_path}")
            raise FileProcessError(
                "File path escapes storage directory",
                file_path=file_path,
            )

        logger.debug(
            f"[FileProcessor] Resolving path: api_path={file_path}, "
            f"local_path={actual_path}, exists={actual_path.exists()}, "
            f"has_remote_storage={self.file_storage is not None}"
        )

        # Check if file exists locally
        if actual_path.exists() and actual_path.is_file():
            logger.info(f"[FileProcessor] File found locally: {actual_path}")
            return actual_path

        # If not local, try downloading from remote storage
        if self.file_storage:
            logger.info(
                f"[FileProcessor] File not found locally, trying remote storage: "
                f"backend={self.file_storage.config.backend.value}"
            )
            downloaded_path = await self._download_from_remote(file_path)
            if downloaded_path and downloaded_path.exists():
                return downloaded_path
            logger.warning(
                f"[FileProcessor] Remote download failed or returned empty for: {file_path}"
            )
        else:
            # No remote storage configured - file might be in S3/OSS but we can't access it
            logger.warning(
                f"[FileProcessor] File not found locally and no remote storage configured: "
                f"{file_path}. If file is in S3/OSS, ensure FILE_STORAGE_BACKEND is configured."
            )

        # File not found anywhere - provide detailed error message
        error_msg = f"File not found: {file_path}"
        if not self.file_storage:
            error_msg += " (remote storage not available - check FILE_STORAGE_BACKEND config)"
        raise FileProcessError(error_msg, file_path=file_path)

    def _resolve_path(self, file_path: str) -> Path:
        """
        Resolve API file path to actual disk path (sync version, local only).

        API path format: /uploads/{user_id}/{file_id}_{timestamp}.{ext}
        Disk path format: {FILE_STORAGE_PATH}/uploads/{user_id}/{file_id}_{timestamp}.{ext}

        Args:
            file_path: File path from API

        Returns:
            Path: Resolved absolute path to the file on disk

        Raises:
            FileProcessError: If the path is invalid or file doesn't exist
        """
        file_path = file_path.strip()

        # Handle paths that start with /uploads/
        if file_path.startswith("/uploads/"):
            relative_path = file_path[1:]  # Remove leading /
            actual_path = self.storage_base_path / relative_path
        elif file_path.startswith("uploads/"):
            actual_path = self.storage_base_path / file_path
        else:
            actual_path = Path(file_path)
            if not actual_path.is_absolute():
                actual_path = self.storage_base_path / file_path

        actual_path = actual_path.resolve()

        # Security check: ensure path is within storage base
        try:
            actual_path.relative_to(self.storage_base_path.resolve())
        except ValueError:
            logger.warning(f"[Security] Path traversal attempt: {file_path}")
            raise FileProcessError(
                "Invalid file path: path must be within storage directory",
                file_path=file_path,
            )

        if not actual_path.exists():
            raise FileProcessError(f"File not found: {file_path}", file_path=file_path)

        if not actual_path.is_file():
            raise FileProcessError(f"Path is not a file: {file_path}", file_path=file_path)

        return actual_path

    def _detect_file_type(self, file_path: Path) -> str:
        """
        Detect file type from extension.

        Args:
            file_path: Path to the file

        Returns:
            "image" | "document" | "unknown"
        """
        ext = file_path.suffix.lower()
        if ext in IMAGE_EXTENSIONS:
            return "image"
        elif ext in DOCUMENT_EXTENSIONS:
            return "document"
        return "unknown"

    def _detect_media_type(self, image_bytes: bytes, ext: str) -> str:
        """
        Detect image media type from magic bytes or extension.

        Args:
            image_bytes: Image file content
            ext: File extension (fallback)

        Returns:
            MIME type string (e.g., "image/png")
        """
        if len(image_bytes) >= 8:
            # Check magic bytes
            if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                return "image/png"
            elif image_bytes[:2] == b"\xff\xd8":
                return "image/jpeg"
            elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
                return "image/gif"
            elif image_bytes[:2] == b"BM":
                return "image/bmp"
            elif (
                image_bytes[:4] == b"RIFF"
                and len(image_bytes) > 12
                and image_bytes[8:12] == b"WEBP"
            ):
                return "image/webp"

        # Fallback to extension
        ext_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }
        return ext_map.get(ext.lower(), "image/png")

    async def _process_image(
        self,
        file_path: Path,
        api_path: str,
        model_supports_vision: bool,
    ) -> tuple[ImageContent | None, str | None, dict[str, Any]]:
        """
        Process an image file.

        Args:
            file_path: Resolved disk path to the image
            api_path: Original API path
            model_supports_vision: Whether the model supports vision

        Returns:
            Tuple of (ImageContent or None, description or None, metadata)
        """
        # Read image bytes
        image_bytes = file_path.read_bytes()
        size_bytes = len(image_bytes)
        ext = file_path.suffix.lower()
        media_type = self._detect_media_type(image_bytes, ext)

        metadata = {
            "file_path": api_path,
            "file_name": file_path.name,
            "file_type": "image",
            "media_type": media_type,
            "size_bytes": size_bytes,
        }

        image_content: ImageContent | None = None
        description: str | None = None

        if model_supports_vision:
            # Convert to base64 for vision models
            b64_data = base64.b64encode(image_bytes).decode("utf-8")
            image_content = ImageContent(
                base64_data=b64_data,
                media_type=media_type,
                file_path=api_path,
                size_bytes=size_bytes,
            )
            logger.debug(
                f"[FileProcessor] Image processed for vision: {api_path}, "
                f"size={size_bytes}, type={media_type}"
            )
        else:
            # Generate description using VLM for text-only models
            if self.vlm_service:
                try:
                    result = await self.vlm_service.describe_image(
                        image_bytes=image_bytes,
                        prompt=self.description_prompt,
                        image_type="general",
                        max_tokens=1000,
                    )
                    description = result.description
                    metadata["vlm_model"] = result.model
                    metadata["vlm_tokens"] = result.tokens_used
                    logger.info(
                        f"[FileProcessor] Image description generated: {api_path}, "
                        f"tokens={result.tokens_used}"
                    )
                except Exception as e:
                    record_internal_exception(
                        __name__, "assistant.core.files.file_processor.internal_failure", e
                    )
                    description = f"[Image: {file_path.name}] (description unavailable)"
                    metadata["vlm_error"] = str(e)
            else:
                # No VLM service, provide placeholder
                description = f"[Image: {file_path.name}]"
                metadata["vlm_skipped"] = "no_vlm_service"

        return image_content, description, metadata

    async def _process_document(
        self,
        file_path: Path,
        api_path: str,
        max_text_chars: int,
    ) -> tuple[str, bool, dict[str, Any]]:
        """
        Process a document file.

        Args:
            file_path: Resolved disk path to the document
            api_path: Original API path
            max_text_chars: Maximum characters for inline text

        Returns:
            Tuple of (text_content, requires_rag, metadata)
        """
        metadata = {
            "file_path": api_path,
            "file_name": file_path.name,
            "file_type": "document",
            "size_bytes": file_path.stat().st_size,
        }

        try:
            # Parse document using DocumentParser
            # Use the resolved file_path (which may be a temp file downloaded from remote storage)
            text = await self.document_parser.parse(str(file_path))
            text_length = len(text)
            metadata["text_length"] = text_length

            if text_length <= max_text_chars:
                # Short document: include inline
                logger.info(
                    f"[FileProcessor] Document parsed (short): {api_path}, length={text_length}"
                )
                return text, False, metadata
            else:
                # Long document: mark for RAG
                logger.info(
                    f"[FileProcessor] Document parsed (long, needs RAG): {api_path}, "
                    f"length={text_length} > {max_text_chars}"
                )
                metadata["requires_rag"] = True
                metadata["truncated_preview"] = text[:1000] + "..."
                return "", True, metadata

        except DocumentParseError as e:
            record_internal_exception(logger, "file_processor.document_parse_failed", e)
            metadata["parse_error"] = "Document parsing failed"
            return "", False, metadata

    def _extract_pdf_text_with_pypdf(
        self,
        file_path: Path,
        api_path: str,
        max_text_chars: int,
    ) -> tuple[str, bool, dict[str, Any]]:
        """Best-effort PDF text fallback when vision conversion/parser fails."""
        metadata: dict[str, Any] = {
            "file_path": api_path,
            "file_name": file_path.name,
            "file_type": "pdf",
            "fallback_mode": "pypdf",
        }
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(file_path))
            text_parts: list[str] = []
            for page in reader.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    text_parts.append(page_text.strip())
            text = "\n\n".join(text_parts).strip()

            metadata["page_count"] = len(reader.pages)
            metadata["text_length"] = len(text)

            if not text:
                metadata["parse_error"] = "No extractable text found in PDF"
                return "", False, metadata

            if len(text) <= max_text_chars:
                return text, False, metadata

            metadata["requires_rag"] = True
            metadata["truncated_preview"] = text[:1000] + "..."
            return "", True, metadata
        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )
            metadata["parse_error"] = f"pypdf fallback failed: {e}"
            return "", False, metadata

    async def _fallback_pdf_to_text(
        self,
        file_path: Path,
        api_path: str,
        max_text_chars: int,
    ) -> tuple[str, bool, dict[str, Any]]:
        """Run PDF text fallback chain: parser first, then pypdf."""
        fallback_text, needs_rag, fallback_meta = await self._process_document(
            file_path=file_path,
            api_path=api_path,
            max_text_chars=max_text_chars,
        )
        if fallback_text or needs_rag:
            return fallback_text, needs_rag, fallback_meta

        fallback_text, needs_rag, pypdf_meta = self._extract_pdf_text_with_pypdf(
            file_path=file_path,
            api_path=api_path,
            max_text_chars=max_text_chars,
        )
        fallback_meta.update(
            {
                "pypdf_fallback": True,
                "pypdf_meta": pypdf_meta,
            }
        )
        return fallback_text, needs_rag, fallback_meta

    def _analyze_document_structure(self, text_content: str) -> DocumentStructure:
        """
        Analyze document structure for deep understanding.

        This lightweight analysis extracts:
        - Document outline (headers and sections)
        - Content characteristics (lists, tables, code blocks)
        - Reading time estimation
        - Key topics from headers

        Args:
            text_content: The extracted text content from document

        Returns:
            DocumentStructure with analysis results
        """
        import re

        lines = text_content.split("\n")
        structure = DocumentStructure(
            total_chars=len(text_content),
            total_lines=len(lines),
        )

        # Detect headers (Markdown style and common patterns)
        header_patterns = [
            (r"^#{1,6}\s+(.+)$", "md"),  # Markdown headers
            (r"^(.+)\n[=]+$", "setext1"),  # Setext H1
            (r"^(.+)\n[-]+$", "setext2"),  # Setext H2
            (r"^[一二三四五六七八九十]+[、.]\s*(.+)$", "cn_num"),  # Chinese numbered
            (r"^第[一二三四五六七八九十]+[章节部分]+\s*(.+)$", "cn_chapter"),  # Chinese chapter
            (r"^\d+[.、]\s*(.+)$", "numbered"),  # Numbered sections
        ]

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            # Check for headers
            for pattern, style in header_patterns:
                match = re.match(pattern, line)
                if match:
                    title = match.group(1).strip() if match.groups() else line.strip("#").strip()
                    level = (
                        1
                        if style in ("setext1", "cn_chapter")
                        else (
                            2
                            if style == "setext2"
                            else (len(line) - len(line.lstrip("#")) if style == "md" else 2)
                        )
                    )
                    structure.sections.append(
                        {
                            "level": level,
                            "title": title[:100],  # Limit title length
                            "line": i + 1,
                        }
                    )
                    structure.has_headers = True
                    if title and len(title) > 2:
                        structure.key_topics.append(title[:50])
                    break

            # Check for lists
            if not structure.has_lists and (
                re.match(r"^[-*+]\s+", line) or re.match(r"^\d+[.)]\s+", line)
            ):
                structure.has_lists = True

            # Check for tables (simple heuristic)
            if not structure.has_tables and "|" in line and line.count("|") >= 2:
                structure.has_tables = True

            # Check for code blocks
            if not structure.has_code_blocks and (line.startswith("```") or line.startswith("~~~")):
                structure.has_code_blocks = True

        # Estimate reading time (assuming 250 words/min for English, 400 chars/min for Chinese)
        # Use a blended estimate
        char_count = structure.total_chars
        structure.estimated_reading_time_min = max(1, char_count // 400)

        # Limit key topics to top 10
        structure.key_topics = structure.key_topics[:10]

        logger.info(
            f"[FileProcessor] Document structure analyzed: "
            f"sections={len(structure.sections)}, "
            f"has_headers={structure.has_headers}, "
            f"has_tables={structure.has_tables}, "
            f"topics={len(structure.key_topics)}"
        )

        return structure

    async def _process_pdf_as_images(
        self,
        file_path: Path,
        api_path: str,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[PDFPageContent], dict[str, Any]]:
        """
        Process a PDF file by converting pages to images for vision models.

        This preserves table structures, embedded images, and layout that would
        be lost with text extraction. Industry-standard approach used by
        Claude, GPT-4V, and Gemini.

        Args:
            file_path: Resolved disk path to the PDF
            api_path: Original API path
            on_progress: Optional callback for progress updates

        Returns:
            Tuple of (list of PDFPageContent, metadata dict)
        """
        metadata = {
            "file_path": api_path,
            "file_name": file_path.name,
            "file_type": "pdf",
            "processing_mode": "vision",
            "size_bytes": file_path.stat().st_size,
        }

        try:
            # Wrap the progress callback to match expected signature
            async def pdf_progress(current: int, total: int, msg: str) -> None:
                if on_progress:
                    await on_progress(api_path, current, total, msg)

            # Convert PDF to images
            result = await self.pdf_converter.convert(
                file_path=str(file_path),
                max_pages=self.max_pdf_pages,
                on_progress=pdf_progress,
            )

            # Convert to PDFPageContent objects
            pdf_pages = []
            for page in result.page_images:
                pdf_pages.append(
                    PDFPageContent(
                        page_number=page.page_number,
                        base64_data=page.base64_data,
                        media_type=page.media_type,
                        width=page.width,
                        height=page.height,
                        size_bytes=page.size_bytes,
                        file_path=api_path,
                    )
                )

            metadata["total_pages"] = result.total_pages
            metadata["converted_pages"] = len(pdf_pages)
            metadata["total_image_size_bytes"] = result.total_size_bytes
            metadata["conversion_time_ms"] = result.conversion_time_ms

            if result.total_pages > self.max_pdf_pages:
                metadata["truncated"] = True
                metadata["truncated_message"] = (
                    f"PDF has {result.total_pages} pages, only first {self.max_pdf_pages} converted"
                )

            logger.info(
                f"[FileProcessor] PDF converted to images: {api_path}, "
                f"pages={len(pdf_pages)}/{result.total_pages}, "
                f"size={result.total_size_bytes / 1024 / 1024:.2f}MB, "
                f"time={result.conversion_time_ms:.0f}ms"
            )

            return pdf_pages, metadata

        except PDFConversionError as e:
            record_internal_exception(logger, "file_processor.pdf_conversion_failed", e)
            metadata["conversion_error"] = "PDF conversion failed"
            return [], metadata

        except Exception as e:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", e
            )
            metadata["conversion_error"] = "Unexpected PDF conversion failure"
            return [], metadata

    async def process_files(
        self,
        file_paths: list[str],
        session_id: str,
        user: UserContext,
        model_supports_vision: bool,
        max_text_chars: int = 32000,
        on_progress: ProgressCallback | None = None,
    ) -> ProcessedFiles:
        """
        Process multiple uploaded files for model consumption.

        This method:
        1. Detects file types (image/document/pdf)
        2. For images:
           - Vision models: Convert to base64
           - Text models: Generate descriptions via VLM
        3. For PDFs (when vision supported):
           - Convert pages to images for accurate analysis
           - Preserves tables, embedded images, and layout
        4. For other documents:
           - Short (< max_text_chars): Extract text inline
           - Long (>= max_text_chars): Mark for RAG retrieval

        Args:
            file_paths: List of file paths from API (e.g., /uploads/user123/abc123.pdf)
            session_id: Current session ID (for session KB if needed)
            user: User context for permission checks
            model_supports_vision: Whether the target model supports vision
            max_text_chars: Maximum characters for inline text (default 32000)
            on_progress: Optional callback for progress updates (file, current, total, message)

        Returns:
            ProcessedFiles with all processed content

        Example:
            result = await processor.process_files(
                file_paths=["/uploads/user/doc.pdf", "/uploads/user/img.png"],
                session_id="sess_123",
                user=user_context,
                model_supports_vision=True,
            )
        """
        result = ProcessedFiles()
        text_parts: list[str] = []
        session_kb_documents: list[str] = []

        # Log file storage status for diagnostics
        logger.info(
            f"[FileProcessor] process_files called: "
            f"file_count={len(file_paths)}, "
            f"has_remote_storage={self.file_storage is not None}, "
            f"storage_backend={self.file_storage.config.backend.value if self.file_storage else 'None'}, "
            f"storage_base_path={self.storage_base_path}"
        )

        for api_path in file_paths:
            # Try cache first
            cache_key = self._get_cache_key(api_path, model_supports_vision)
            cached = await self._get_cached_result(cache_key)

            if cached:
                logger.info(f"[FileProcessor] Cache hit for {api_path}")
                # Restore from cache
                if cached.get("images"):
                    for img in cached["images"]:
                        result.images.append(ImageContent(**img))
                if cached.get("pdf_pages"):
                    for page in cached["pdf_pages"]:
                        result.pdf_pages.append(PDFPageContent(**page))
                if cached.get("text_content"):
                    text_parts.append(f"### {Path(api_path).name}\n{cached['text_content']}")
                if cached.get("image_descriptions"):
                    result.image_descriptions.extend(cached["image_descriptions"])
                if cached.get("requires_rag"):
                    result.requires_rag = True
                    cached_metadata = cached.get("metadata") or {}
                    session_kb_documents.append(cached_metadata.get("file_path") or api_path)
                result.file_metadata.append(cached.get("metadata", {}))
                continue

            # Cache miss, process normally
            try:
                # Resolve path (supports remote storage download)
                actual_path = await self._resolve_path_async(api_path)
                file_type = self._detect_file_type(actual_path)
                is_pdf = actual_path.suffix.lower() == ".pdf"

                if file_type == "image":
                    image, description, metadata = await self._process_image(
                        file_path=actual_path,
                        api_path=api_path,
                        model_supports_vision=model_supports_vision,
                    )
                    if image:
                        result.images.append(image)
                    if description:
                        result.image_descriptions.append(description)
                    result.file_metadata.append(metadata)

                elif is_pdf and model_supports_vision:
                    # PDF with vision model: convert to images for best accuracy
                    # This preserves tables, embedded images, and layout
                    pdf_pages, metadata = await self._process_pdf_as_images(
                        file_path=actual_path,
                        api_path=api_path,
                        on_progress=on_progress,
                    )
                    result.pdf_pages.extend(pdf_pages)

                    if not pdf_pages:
                        logger.warning(
                            "[FileProcessor] PDF image conversion produced no pages, fallback to text: %s",
                            api_path,
                        )
                        fallback_text, needs_rag, fallback_meta = await self._fallback_pdf_to_text(
                            file_path=actual_path,
                            api_path=api_path,
                            max_text_chars=max_text_chars,
                        )

                        if fallback_text:
                            text_parts.append(f"### {actual_path.name}\n{fallback_text}")
                        if needs_rag:
                            result.requires_rag = True
                            session_kb_documents.append(api_path)
                        metadata.update(
                            {
                                "fallback_mode": "text",
                                "fallback_text_length": len(fallback_text),
                                "fallback_requires_rag": needs_rag,
                                "fallback_meta": fallback_meta,
                            }
                        )

                    result.file_metadata.append(metadata)

                    logger.info(
                        f"[FileProcessor] PDF processed as images: {api_path}, "
                        f"pages={len(pdf_pages)}"
                    )

                elif file_type == "document":
                    # Non-PDF documents or PDF without vision: use text extraction
                    text, needs_rag, metadata = await self._process_document(
                        file_path=actual_path,
                        api_path=api_path,
                        max_text_chars=max_text_chars,
                    )
                    if text:
                        text_parts.append(f"### {actual_path.name}\n{text}")
                    if needs_rag:
                        result.requires_rag = True
                        session_kb_documents.append(api_path)
                    result.file_metadata.append(metadata)

                else:
                    # Unknown file type
                    logger.warning(f"[FileProcessor] Unknown file type: {api_path}")
                    result.file_metadata.append(
                        {
                            "file_path": api_path,
                            "file_name": actual_path.name,
                            "file_type": "unknown",
                            "error": "Unsupported file type",
                        }
                    )

            except FileProcessError as e:
                record_internal_exception(logger, "file_processor.file_processing_failed", e)
                file_name = Path(api_path).name
                result.file_metadata.append(
                    {
                        "file_path": api_path,
                        "file_name": file_name,
                        "error": "File processing failed",
                    }
                )
                # Add error as text content so model can explain to user
                error_text = f"[文件处理失败: {file_name}]"
                text_parts.append(error_text)
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.files.file_processor.internal_failure", e
                )
                file_name = Path(api_path).name
                result.file_metadata.append(
                    {
                        "file_path": api_path,
                        "file_name": file_name,
                        "error": f"Unexpected error: {str(e)}",
                    }
                )
                # Add error as text content so model can explain to user
                error_text = f"[文件处理失败: {file_name}]\n错误: {str(e)}"
                text_parts.append(error_text)

        # Combine text parts
        if text_parts:
            result.text_content = "\n\n".join(text_parts)

        # Perform document structure analysis for deep understanding
        # This helps the AI provide better analysis of uploaded documents
        if result.text_content:
            try:
                result.document_structure = self._analyze_document_structure(result.text_content)
            except Exception as e:
                record_internal_exception(
                    __name__, "assistant.core.files.file_processor.internal_failure", e
                )

        if result.requires_rag and session_kb_documents:
            result.session_kb_id = await self.create_session_kb(
                session_id=session_id,
                user=user,
                documents=session_kb_documents,
            )

        logger.info(
            f"[FileProcessor] Processed {len(file_paths)} files: "
            f"images={len(result.images)}, descriptions={len(result.image_descriptions)}, "
            f"text_chars={len(result.text_content)}, requires_rag={result.requires_rag}, "
            f"has_structure={result.document_structure is not None}"
        )

        return result

    async def process_file_for_provider(
        self,
        file_path: str,
        provider: str,
        model_id: str,
        content_type: str,
        google_api_key: str | None = None,
        force_strategy: ProcessingStrategy | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ProcessedContent:
        """
        Process a single file using provider-specific strategy.

        This method selects the best processing strategy based on the target
        model provider:
        - Google/Gemini: Uses native File API for optimal performance
        - Anthropic/Claude: Converts PDFs to images for vision models
        - OpenAI/GPT-4: Converts PDFs to images for vision models
        - Others: Falls back to text extraction

        Args:
            file_path: API path to the file (e.g., /uploads/user/doc.pdf)
            provider: Model provider (google, anthropic, openai, etc.)
            model_id: Model identifier
            content_type: MIME type of the file
            google_api_key: Google API key for Gemini File API
            force_strategy: Force a specific processing strategy
            on_progress: Progress callback (file, current, total, message)

        Returns:
            ProcessedContent with the processed file data

        Example:
            # For Gemini - uses native File API
            result = await processor.process_file_for_provider(
                file_path="/uploads/user/doc.pdf",
                provider="google",
                model_id="gemini-3-flash-preview",
                content_type="application/pdf",
                google_api_key="YOUR_API_KEY",
            )
            # result.gemini_file_uri is set

            # For Claude - converts to images
            result = await processor.process_file_for_provider(
                file_path="/uploads/user/doc.pdf",
                provider="anthropic",
                model_id="claude-3-sonnet",
                content_type="application/pdf",
            )
            # result.image_blocks contains page images
        """
        factory = FileProcessingStrategyFactory(
            google_api_key=google_api_key,
            storage_base_path=self.storage_base_path,
        )

        strategy = factory.get_strategy(
            provider=provider,
            model_id=model_id,
            content_type=content_type,
            force_strategy=force_strategy,
        )

        # Wrap progress callback
        async def strategy_progress(current: int, total: int, msg: str) -> None:
            if on_progress:
                await on_progress(file_path, current, total, msg)

        result = await strategy.process(
            file_path=file_path,
            content_type=content_type,
            on_progress=strategy_progress,
        )

        logger.info(
            f"[FileProcessor] Processed {file_path} for {provider}/{model_id}: "
            f"strategy={result.strategy.value}, "
            f"success={result.is_success}, "
            f"time={result.processing_time_ms:.0f}ms"
        )

        return result

    async def create_session_kb(
        self,
        session_id: str,
        user: UserContext,
        documents: list[str],
    ) -> str | None:
        """
        Create a session-level temporary knowledge base for long documents.

        Args:
            session_id: Session ID for the temporary KB
            user: User context
            documents: List of document paths to add to KB

        Returns:
            Session KB ID if created, None otherwise
        """
        if not self.knowledge_service:
            logger.warning("[FileProcessor] Cannot create session KB: no knowledge service")
            return None

        if not documents:
            return None

        logger.info(
            f"[FileProcessor] Session KB creation requested: "
            f"session={session_id}, documents={len(documents)}"
        )

        create_dataset = getattr(self.knowledge_service, "create_session_dataset", None)
        if not callable(create_dataset):
            logger.warning(
                "[FileProcessor] Cannot create session KB: knowledge service "
                "does not expose create_session_dataset"
            )
            return None

        metadata = {
            "source_type": "session_file",
            "freshness": "session",
            "scope": {
                "tenant_id": getattr(user, "tenant_id", ""),
                "user_id": getattr(user, "user_id", ""),
                "session_id": session_id,
            },
        }

        try:
            created = create_dataset(
                user=user,
                session_id=session_id,
                documents=documents,
                metadata=metadata,
            )
            if inspect.isawaitable(created):
                created = await created
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.files.file_processor.internal_failure", exc
            )
            return None

        return self._extract_session_kb_id(created)

    @staticmethod
    def _extract_session_kb_id(created: Any) -> str | None:
        """Extract a dataset ID from supported session-KB proxy responses."""
        if isinstance(created, str):
            return created
        if isinstance(created, dict):
            for key in ("dataset_id", "kb_id", "id"):
                value = created.get(key)
                if value:
                    return str(value)
            return None
        for attr in ("dataset_id", "kb_id", "id"):
            value = getattr(created, attr, None)
            if value:
                return str(value)
        return None


def create_file_processor(
    vlm_service: Any | None = None,
    knowledge_service: KnowledgeClientLike | None = None,
    storage_base_path: Path | None = None,
    file_storage: FileStorageLike | None = None,
    redis_client: Any | None = None,
) -> FileProcessor:
    """
    Factory function to create a FileProcessor instance.

    Args:
        vlm_service: VLM service for image descriptions
        knowledge_service: Knowledge service for session KB
        storage_base_path: Base path for file storage
        file_storage: FileStorageLike for remote storage (S3/OSS)
        redis_client: Redis client for caching

    Returns:
        Configured FileProcessor instance
    """
    return FileProcessor(
        vlm_service=vlm_service,
        knowledge_service=knowledge_service,
        storage_base_path=storage_base_path,
        file_storage=file_storage,
        redis_client=redis_client,
    )
