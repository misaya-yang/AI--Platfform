"""
File Processing Strategy Pattern.

Provides different file processing strategies for different LLM providers:
- GeminiFileStrategy: Uses Gemini's native File API for document processing
- VisionModelStrategy: Converts PDFs to images for Claude/GPT-4V vision models
- TextExtractionStrategy: Extracts text content for text-only models

This allows the AI assistant to use the best approach for each model/provider.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable, Dict, List, Optional

from ...core.observability.logging import get_logger

if TYPE_CHECKING:
    from .model_registry import ModelRegistry

logger = get_logger(__name__)


class ProcessingStrategy(str, Enum):
    """Available file processing strategies."""
    GEMINI_FILE_API = "gemini_file_api"  # Gemini native File API
    VISION_IMAGES = "vision_images"  # Convert to images for vision models
    TEXT_EXTRACTION = "text_extraction"  # Extract text content


@dataclass
class ProcessedContent:
    """Result of file processing."""
    strategy: ProcessingStrategy
    file_path: str
    original_filename: str

    # For text extraction
    text_content: Optional[str] = None

    # For vision/image processing
    image_blocks: List[Dict[str, Any]] = field(default_factory=list)

    # For Gemini File API
    gemini_file_uri: Optional[str] = None
    gemini_file_state: Optional[str] = None  # PROCESSING, ACTIVE, FAILED

    # Metadata
    total_pages: int = 0
    total_size_bytes: int = 0
    processing_time_ms: float = 0
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        """Check if processing was successful."""
        return self.error is None

    @property
    def has_content(self) -> bool:
        """Check if any content was extracted."""
        return bool(self.text_content or self.image_blocks or self.gemini_file_uri)


# Type alias for progress callback
ProgressCallback = Callable[[int, int, str], Awaitable[None]]


class FileProcessingStrategy(ABC):
    """Abstract base class for file processing strategies."""

    @property
    @abstractmethod
    def strategy_type(self) -> ProcessingStrategy:
        """Return the strategy type."""
        pass

    @abstractmethod
    async def process(
        self,
        file_path: str,
        content_type: str,
        on_progress: Optional[ProgressCallback] = None,
        **kwargs,
    ) -> ProcessedContent:
        """
        Process a file and return the processed content.

        Args:
            file_path: Path to the file (API path like /uploads/user/file.pdf)
            content_type: MIME type of the file
            on_progress: Optional progress callback
            **kwargs: Strategy-specific options

        Returns:
            ProcessedContent with the processed content
        """
        pass

    def supports_file_type(self, content_type: str) -> bool:
        """Check if this strategy supports the given file type."""
        return True  # Override in subclasses if needed


class TextExtractionStrategy(FileProcessingStrategy):
    """
    Extract text content from documents using unstructured library.

    Best for: Text-only models, quick document analysis
    Pros: Fast, low token usage
    Cons: Loses formatting, tables, images
    """

    def __init__(self, storage_base_path: Optional[Path] = None):
        """
        Initialize text extraction strategy.

        Args:
            storage_base_path: Base path for file storage
        """
        self.storage_base_path = storage_base_path

    @property
    def strategy_type(self) -> ProcessingStrategy:
        return ProcessingStrategy.TEXT_EXTRACTION

    async def process(
        self,
        file_path: str,
        content_type: str,
        on_progress: Optional[ProgressCallback] = None,
        **kwargs,
    ) -> ProcessedContent:
        """Extract text from document."""
        import time
        start_time = time.time()

        result = ProcessedContent(
            strategy=self.strategy_type,
            file_path=file_path,
            original_filename=Path(file_path).name,
        )

        try:
            from .document_parser import DocumentParser

            parser = DocumentParser(storage_base_path=self.storage_base_path)
            text = await parser.parse(file_path)

            result.text_content = text
            result.total_size_bytes = len(text.encode("utf-8"))

            if on_progress:
                await on_progress(1, 1, "Text extraction complete")

        except Exception as e:
            logger.error(f"[TextExtraction] Failed to extract text from {file_path}: {e}")
            result.error = str(e)

        result.processing_time_ms = (time.time() - start_time) * 1000
        return result


class VisionModelStrategy(FileProcessingStrategy):
    """
    Convert PDF pages to images for vision models (Claude, GPT-4V).

    Best for: Complex documents with tables, charts, images
    Pros: Preserves visual layout, supports all document elements
    Cons: Higher token usage (~258 tokens/page)
    """

    def __init__(
        self,
        dpi: int = 150,
        max_pages: Optional[int] = None,
        provider: str = "openai",
        storage_base_path: Optional[Path] = None,
    ):
        """
        Initialize vision model strategy.

        Args:
            dpi: Rendering resolution (default 150)
            max_pages: Maximum pages to convert (default: all)
            provider: Target provider format (openai, anthropic)
            storage_base_path: Base path for file storage
        """
        self.dpi = dpi
        self.max_pages = max_pages
        self.provider = provider
        self.storage_base_path = storage_base_path

    @property
    def strategy_type(self) -> ProcessingStrategy:
        return ProcessingStrategy.VISION_IMAGES

    def supports_file_type(self, content_type: str) -> bool:
        """Only supports PDFs for conversion."""
        return content_type == "application/pdf"

    async def process(
        self,
        file_path: str,
        content_type: str,
        on_progress: Optional[ProgressCallback] = None,
        **kwargs,
    ) -> ProcessedContent:
        """Convert PDF to images."""
        import time
        start_time = time.time()

        result = ProcessedContent(
            strategy=self.strategy_type,
            file_path=file_path,
            original_filename=Path(file_path).name,
        )

        # Handle non-PDF files by delegating to text extraction
        if content_type != "application/pdf":
            # Fall back to text extraction for non-PDFs
            text_strategy = TextExtractionStrategy(self.storage_base_path)
            return await text_strategy.process(file_path, content_type, on_progress, **kwargs)

        try:
            from .pdf_converter import PDFConverter
            from .document_parser import DocumentParser

            # Resolve file path
            parser = DocumentParser(storage_base_path=self.storage_base_path)
            actual_path = str(parser._resolve_path(file_path))

            converter = PDFConverter(dpi=self.dpi)
            conversion_result = await converter.convert(
                file_path=actual_path,
                max_pages=self.max_pages,
                on_progress=on_progress,
            )

            result.total_pages = conversion_result.total_pages
            result.total_size_bytes = conversion_result.total_size_bytes

            # Convert to provider-specific format
            for page in conversion_result.page_images:
                if self.provider == "anthropic":
                    result.image_blocks.append(page.to_anthropic_format())
                else:
                    result.image_blocks.append(page.to_openai_format())

            logger.info(
                f"[VisionStrategy] Converted {file_path}: {result.total_pages} pages, "
                f"{result.total_size_bytes / 1024 / 1024:.2f}MB"
            )

        except Exception as e:
            logger.error(f"[VisionStrategy] Failed to process {file_path}: {e}")
            result.error = str(e)

        result.processing_time_ms = (time.time() - start_time) * 1000
        return result


class GeminiFileStrategy(FileProcessingStrategy):
    """
    Use Gemini's native File API for document processing.

    Best for: All Gemini models, native document understanding
    Pros: Native PDF support, optimized for Gemini, supports large files
    Cons: Only works with Gemini models, requires API upload
    """

    def __init__(
        self,
        api_key: str,
        storage_base_path: Optional[Path] = None,
    ):
        """
        Initialize Gemini file strategy.

        Args:
            api_key: Google API key
            storage_base_path: Base path for file storage
        """
        self.api_key = api_key
        self.storage_base_path = storage_base_path
        self._client = None

    @property
    def strategy_type(self) -> ProcessingStrategy:
        return ProcessingStrategy.GEMINI_FILE_API

    async def _get_client(self):
        """Get or create Gemini client."""
        if self._client is None:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self._client = genai
            except ImportError:
                raise ImportError(
                    "google-generativeai is required for Gemini File API. "
                    "Install with: pip install google-generativeai"
                )
        return self._client

    async def process(
        self,
        file_path: str,
        content_type: str,
        on_progress: Optional[ProgressCallback] = None,
        **kwargs,
    ) -> ProcessedContent:
        """Upload file to Gemini and get file URI."""
        import time
        start_time = time.time()

        result = ProcessedContent(
            strategy=self.strategy_type,
            file_path=file_path,
            original_filename=Path(file_path).name,
        )

        try:
            from .document_parser import DocumentParser

            # Resolve file path
            parser = DocumentParser(storage_base_path=self.storage_base_path)
            actual_path = str(parser._resolve_path(file_path))

            if on_progress:
                await on_progress(0, 2, "Uploading to Gemini...")

            # Upload file to Gemini
            genai = await self._get_client()

            # Use asyncio.to_thread for sync Gemini API
            def upload_file():
                return genai.upload_file(
                    path=actual_path,
                    mime_type=content_type,
                    display_name=result.original_filename,
                )

            uploaded_file = await asyncio.to_thread(upload_file)

            if on_progress:
                await on_progress(1, 2, "Processing file...")

            # Wait for file to be processed
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)

                def get_file_state():
                    return genai.get_file(uploaded_file.name)

                uploaded_file = await asyncio.to_thread(get_file_state)

            if uploaded_file.state.name == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {uploaded_file.state.name}")

            result.gemini_file_uri = uploaded_file.uri
            result.gemini_file_state = uploaded_file.state.name
            result.total_size_bytes = Path(actual_path).stat().st_size

            if on_progress:
                await on_progress(2, 2, "File ready")

            logger.info(
                f"[GeminiStrategy] Uploaded {file_path} -> {uploaded_file.uri} "
                f"(state={uploaded_file.state.name})"
            )

        except Exception as e:
            logger.error(f"[GeminiStrategy] Failed to process {file_path}: {e}")
            result.error = str(e)

        result.processing_time_ms = (time.time() - start_time) * 1000
        return result


class FileProcessingStrategyFactory:
    """
    Factory for creating file processing strategies.

    Automatically selects the best strategy based on model provider and file type.
    """

    def __init__(
        self,
        google_api_key: Optional[str] = None,
        storage_base_path: Optional[Path] = None,
        model_registry: Optional["ModelRegistry"] = None,
    ):
        """
        Initialize the strategy factory.

        Args:
            google_api_key: API key for Gemini (enables Gemini File API)
            storage_base_path: Base path for file storage
            model_registry: Optional ModelRegistry for dynamic vision model detection
        """
        self.google_api_key = google_api_key
        self.storage_base_path = storage_base_path
        self.model_registry = model_registry

    def get_strategy(
        self,
        provider: str,
        model_id: str,
        content_type: str,
        force_strategy: Optional[ProcessingStrategy] = None,
    ) -> FileProcessingStrategy:
        """
        Get the best strategy for the given parameters.

        Args:
            provider: Model provider (google, anthropic, openai, etc.)
            model_id: Model ID
            content_type: File MIME type
            force_strategy: Force a specific strategy

        Returns:
            FileProcessingStrategy instance
        """
        # If strategy is forced, use it
        if force_strategy:
            return self._create_strategy(force_strategy, provider)

        # Gemini models: prefer native File API
        if provider == "google" and self.google_api_key:
            return GeminiFileStrategy(
                api_key=self.google_api_key,
                storage_base_path=self.storage_base_path,
            )

        # Vision models (Claude, GPT-4V): use image conversion for PDFs
        if content_type == "application/pdf" and self._is_vision_model(provider, model_id):
            return VisionModelStrategy(
                provider="anthropic" if provider == "anthropic" else "openai",
                storage_base_path=self.storage_base_path,
            )

        # Default: text extraction
        return TextExtractionStrategy(storage_base_path=self.storage_base_path)

    def _create_strategy(
        self,
        strategy: ProcessingStrategy,
        provider: str,
    ) -> FileProcessingStrategy:
        """Create a specific strategy."""
        if strategy == ProcessingStrategy.GEMINI_FILE_API:
            if not self.google_api_key:
                raise ValueError("Google API key required for Gemini File API strategy")
            return GeminiFileStrategy(
                api_key=self.google_api_key,
                storage_base_path=self.storage_base_path,
            )
        elif strategy == ProcessingStrategy.VISION_IMAGES:
            return VisionModelStrategy(
                provider="anthropic" if provider == "anthropic" else "openai",
                storage_base_path=self.storage_base_path,
            )
        else:
            return TextExtractionStrategy(storage_base_path=self.storage_base_path)

    def _is_vision_model(self, provider: str, model_id: str) -> bool:
        """Check if the model supports vision/images.

        First checks the model registry if available, then falls back
        to a hardcoded list for backwards compatibility.
        """
        # Try to get from model registry first (dynamic, up-to-date)
        if self.model_registry:
            model_info = self.model_registry.get_model(model_id)
            if model_info:
                return model_info.supports_vision

        # Fallback: hardcoded list for when registry is not available
        # This list should be kept in sync with model_registry.py
        vision_model_prefixes = {
            # OpenAI
            "gpt-4-vision-preview",
            "gpt-4-turbo",
            "gpt-4o",
            "gpt-4o-mini",
            "o1",
            # Anthropic
            "claude-3-opus",
            "claude-3-sonnet",
            "claude-3-haiku",
            "claude-3-5-sonnet",
            "claude-3-5-haiku",
            "claude-sonnet-4",
            # Qwen
            "qwen-vl-max",
            "qwen-vl-plus",
            # Gemini
            "gemini-2",
            "gemini-3",
        }

        # Check exact match or prefix match
        model_lower = model_id.lower()
        for vm in vision_model_prefixes:
            if model_lower == vm or model_lower.startswith(vm):
                return True

        return False
