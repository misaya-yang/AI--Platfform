"""
File Processor for Upload Analysis.

Processes uploaded files and converts them to model-consumable formats:
- Images: Convert to base64 for vision models, or generate descriptions for text models
- Documents: Extract text content, create session KB for long documents

This is a core component of the file upload analysis feature, integrating:
- DocumentParser for document parsing
- VLM service for image descriptions
- KnowledgeService for session-level temporary KB (long documents)
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ...core.observability.logging import get_logger
from ...core.auth.user_resolver import UserContext
from .document_parser import DocumentParser, DocumentParseError

if TYPE_CHECKING:
    from ..knowledge.vlm_service import DashScopeVLMService
    from ..knowledge.knowledge_service import KnowledgeService

logger = get_logger(__name__)

# Default storage path (matches files.py configuration)
FILE_STORAGE_PATH = Path(os.getenv("FILE_STORAGE_PATH", "./uploads")).expanduser()

# Supported image extensions
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

# Supported document extensions (from DocumentParser)
DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".html", ".htm"
})


class FileProcessError(Exception):
    """Raised when file processing fails."""

    def __init__(
        self,
        message: str,
        file_path: str,
        original_error: Optional[Exception] = None,
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

    def to_openai_format(self) -> Dict[str, Any]:
        """Convert to OpenAI vision API format."""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{self.media_type};base64,{self.base64_data}",
            },
        }

    def to_anthropic_format(self) -> Dict[str, Any]:
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
class ProcessedFiles:
    """Result of file processing for model consumption.

    Attributes:
        images: List of base64-encoded images for vision models
        text_content: Extracted text from short documents (< max_text_chars)
        image_descriptions: VLM-generated descriptions for text-only models
        session_kb_id: ID for session-level temporary KB (long documents)
        file_metadata: Metadata about each processed file
        requires_rag: Flag indicating long documents need RAG retrieval
    """
    images: List[ImageContent] = field(default_factory=list)
    text_content: str = ""
    image_descriptions: List[str] = field(default_factory=list)
    session_kb_id: Optional[str] = None
    file_metadata: List[Dict[str, Any]] = field(default_factory=list)
    requires_rag: bool = False

    @property
    def has_images(self) -> bool:
        """Check if there are any images."""
        return len(self.images) > 0

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

    def __init__(
        self,
        vlm_service: Optional["DashScopeVLMService"] = None,
        knowledge_service: Optional["KnowledgeService"] = None,
        storage_base_path: Optional[Path] = None,
        use_english_prompt: bool = False,
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
        """
        self.vlm_service = vlm_service
        self.knowledge_service = knowledge_service
        self.storage_base_path = storage_base_path or FILE_STORAGE_PATH
        self.document_parser = DocumentParser(storage_base_path=self.storage_base_path)
        self.description_prompt = (
            self.IMAGE_DESCRIPTION_PROMPT_EN if use_english_prompt
            else self.IMAGE_DESCRIPTION_PROMPT
        )

    def _resolve_path(self, file_path: str) -> Path:
        """
        Resolve API file path to actual disk path.

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
            if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                return "image/png"
            elif image_bytes[:2] == b'\xff\xd8':
                return "image/jpeg"
            elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
                return "image/gif"
            elif image_bytes[:2] == b'BM':
                return "image/bmp"
            elif image_bytes[:4] == b'RIFF' and len(image_bytes) > 12 and image_bytes[8:12] == b'WEBP':
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
    ) -> tuple[Optional[ImageContent], Optional[str], Dict[str, Any]]:
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

        image_content: Optional[ImageContent] = None
        description: Optional[str] = None

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
                    logger.warning(
                        f"[FileProcessor] Failed to generate image description: {e}"
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
    ) -> tuple[str, bool, Dict[str, Any]]:
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
            text = await self.document_parser.parse(api_path)
            text_length = len(text)
            metadata["text_length"] = text_length

            if text_length <= max_text_chars:
                # Short document: include inline
                logger.info(
                    f"[FileProcessor] Document parsed (short): {api_path}, "
                    f"length={text_length}"
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
            logger.error(f"[FileProcessor] Document parse error: {e}")
            metadata["parse_error"] = str(e)
            return "", False, metadata

    async def process_files(
        self,
        file_paths: List[str],
        session_id: str,
        user: UserContext,
        model_supports_vision: bool,
        max_text_chars: int = 32000,
    ) -> ProcessedFiles:
        """
        Process multiple uploaded files for model consumption.

        This method:
        1. Detects file types (image/document)
        2. For images:
           - Vision models: Convert to base64
           - Text models: Generate descriptions via VLM
        3. For documents:
           - Short (< max_text_chars): Extract text inline
           - Long (>= max_text_chars): Mark for RAG retrieval

        Args:
            file_paths: List of file paths from API (e.g., /uploads/user123/abc123.pdf)
            session_id: Current session ID (for session KB if needed)
            user: User context for permission checks
            model_supports_vision: Whether the target model supports vision
            max_text_chars: Maximum characters for inline text (default 32000)

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
        text_parts: List[str] = []

        for api_path in file_paths:
            try:
                # Resolve path
                actual_path = self._resolve_path(api_path)
                file_type = self._detect_file_type(actual_path)

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

                elif file_type == "document":
                    text, needs_rag, metadata = await self._process_document(
                        file_path=actual_path,
                        api_path=api_path,
                        max_text_chars=max_text_chars,
                    )
                    if text:
                        text_parts.append(f"### {actual_path.name}\n{text}")
                    if needs_rag:
                        result.requires_rag = True
                    result.file_metadata.append(metadata)

                else:
                    # Unknown file type
                    logger.warning(f"[FileProcessor] Unknown file type: {api_path}")
                    result.file_metadata.append({
                        "file_path": api_path,
                        "file_name": actual_path.name,
                        "file_type": "unknown",
                        "error": "Unsupported file type",
                    })

            except FileProcessError as e:
                logger.error(f"[FileProcessor] Error processing file: {e}")
                result.file_metadata.append({
                    "file_path": api_path,
                    "error": str(e),
                })
            except Exception as e:
                logger.exception(f"[FileProcessor] Unexpected error: {api_path}")
                result.file_metadata.append({
                    "file_path": api_path,
                    "error": f"Unexpected error: {str(e)}",
                })

        # Combine text parts
        if text_parts:
            result.text_content = "\n\n".join(text_parts)

        logger.info(
            f"[FileProcessor] Processed {len(file_paths)} files: "
            f"images={len(result.images)}, descriptions={len(result.image_descriptions)}, "
            f"text_chars={len(result.text_content)}, requires_rag={result.requires_rag}"
        )

        return result

    async def create_session_kb(
        self,
        session_id: str,
        user: UserContext,
        documents: List[str],
    ) -> Optional[str]:
        """
        Create a session-level temporary knowledge base for long documents.

        This is a placeholder for future implementation. Currently returns None
        and logs the intent.

        Args:
            session_id: Session ID for the temporary KB
            user: User context
            documents: List of document paths to add to KB

        Returns:
            Session KB ID if created, None otherwise
        """
        # TODO: Implement session KB creation using KnowledgeService
        # This would:
        # 1. Create a temporary dataset with session_id as prefix
        # 2. Add documents to the dataset
        # 3. Return the dataset_id for RAG retrieval

        if not self.knowledge_service:
            logger.warning(
                "[FileProcessor] Cannot create session KB: no knowledge service"
            )
            return None

        logger.info(
            f"[FileProcessor] Session KB creation requested: "
            f"session={session_id}, documents={len(documents)}"
        )

        # Placeholder: return None, actual KB creation to be implemented
        # in a future task that integrates with KnowledgeService
        return None


def create_file_processor(
    vlm_service: Optional["DashScopeVLMService"] = None,
    knowledge_service: Optional["KnowledgeService"] = None,
    storage_base_path: Optional[Path] = None,
) -> FileProcessor:
    """
    Factory function to create a FileProcessor instance.

    Args:
        vlm_service: VLM service for image descriptions
        knowledge_service: Knowledge service for session KB
        storage_base_path: Base path for file storage

    Returns:
        Configured FileProcessor instance
    """
    return FileProcessor(
        vlm_service=vlm_service,
        knowledge_service=knowledge_service,
        storage_base_path=storage_base_path,
    )
