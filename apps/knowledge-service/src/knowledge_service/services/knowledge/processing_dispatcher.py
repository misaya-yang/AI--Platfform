"""
Processing Dispatcher - Routes documents to appropriate processors

Routes documents to the correct processor based on processing_mode:
- text_only: Traditional OCR + text embedding
- scanned: VisionPDFProcessor (Page-as-Image)
- multimodal: Mixed text + image processing
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .processing_mode import ProcessingMode
from .vision_pdf_processor import ProcessingResult, VisionPDFProcessor

logger = logging.getLogger(__name__)


@dataclass
class DispatchContext:
    """Context passed to processors."""

    document_id: str
    dataset_id: str
    content: bytes
    filename: str
    mime_type: str
    collection: str
    tenant_id: str
    on_progress: Callable[[int, int], Awaitable[None]] | None = None
    storage_service: Any | None = None


class ProcessingDispatcher:
    """
    Dispatches document processing to the appropriate processor
    based on the selected processing mode.
    """

    def __init__(
        self,
        vision_processor: VisionPDFProcessor | None = None,
        text_processor: Any | None = None,  # Legacy text processor
        multimodal_processor: Any | None = None,  # Legacy multimodal processor
        knowledge_service: Any | None = None,  # For fallback to existing logic
    ):
        """
        Initialize dispatcher with available processors.

        Args:
            vision_processor: VisionPDFProcessor for scanned mode
            text_processor: Processor for text_only mode
            multimodal_processor: Processor for multimodal mode
            knowledge_service: KnowledgeService for fallback processing
        """
        self.vision_processor = vision_processor
        self.text_processor = text_processor
        self.multimodal_processor = multimodal_processor
        self.knowledge_service = knowledge_service

    async def dispatch(
        self,
        mode: ProcessingMode,
        context: DispatchContext,
    ) -> ProcessingResult:
        """
        Dispatch processing to the appropriate processor.

        Args:
            mode: Processing mode (text_only, scanned, multimodal)
            context: Processing context with document info

        Returns:
            ProcessingResult with status and counts
        """
        logger.info(
            f"[Dispatcher] Processing {context.document_id} with mode={mode.value}, "
            f"file={context.filename}, mime={context.mime_type}"
        )

        try:
            if mode == ProcessingMode.SCANNED:
                return await self._process_scanned(context)
            elif mode == ProcessingMode.TEXT_ONLY:
                return await self._process_text_only(context)
            elif mode == ProcessingMode.MULTIMODAL:
                return await self._process_multimodal(context)
            else:
                # Fallback to text_only for unknown modes
                logger.warning(f"[Dispatcher] Unknown mode {mode}, falling back to text_only")
                return await self._process_text_only(context)

        except Exception as e:
            logger.error(f"[Dispatcher] Processing failed: {e}")
            return ProcessingResult(
                success=False,
                total_pages=0,
                processed_pages=0,
                failed_pages=0,
                error=str(e),
            )

    async def _process_scanned(self, context: DispatchContext) -> ProcessingResult:
        """Process document using vision-first approach (Page-as-Image)."""

        if not self.vision_processor:
            raise ValueError("VisionPDFProcessor not initialized")

        # Only PDFs are supported for scanned mode
        if not context.filename.lower().endswith(".pdf"):
            logger.warning(
                f"[Dispatcher] Scanned mode only supports PDF, "
                f"got {context.filename}, falling back to text_only"
            )
            return await self._process_text_only(context)

        return await self.vision_processor.process(
            pdf_bytes=context.content,
            document_id=context.document_id,
            dataset_id=context.dataset_id,
            collection=context.collection,
            on_progress=context.on_progress,
            storage_service=context.storage_service,
            tenant_id=context.tenant_id,
        )

    async def _process_text_only(self, context: DispatchContext) -> ProcessingResult:
        """Process document using traditional OCR + text embedding."""

        # Use existing KnowledgeService logic for text processing
        # This maintains backward compatibility
        if self.knowledge_service:
            try:
                # Call the existing ingest_document logic
                # The existing logic handles OCR, chunking, and text embedding
                await self.knowledge_service._ingest_document_internal(
                    document_id=context.document_id,
                    dataset_id=context.dataset_id,
                )

                return ProcessingResult(
                    success=True,
                    total_pages=0,  # Text mode doesn't track pages
                    processed_pages=0,
                    failed_pages=0,
                )
            except Exception as e:
                logger.error(f"[Dispatcher] Text processing failed: {e}")
                return ProcessingResult(
                    success=False,
                    total_pages=0,
                    processed_pages=0,
                    failed_pages=0,
                    error=str(e),
                )

        raise ValueError("No text processor available")

    async def _process_multimodal(self, context: DispatchContext) -> ProcessingResult:
        """Process document using combined text + image embedding."""

        # Use existing KnowledgeService multimodal logic
        # This maintains backward compatibility with the current implementation
        if self.knowledge_service:
            try:
                await self.knowledge_service._ingest_document_internal(
                    document_id=context.document_id,
                    dataset_id=context.dataset_id,
                )

                return ProcessingResult(
                    success=True,
                    total_pages=0,
                    processed_pages=0,
                    failed_pages=0,
                )
            except Exception as e:
                logger.error(f"[Dispatcher] Multimodal processing failed: {e}")
                return ProcessingResult(
                    success=False,
                    total_pages=0,
                    processed_pages=0,
                    failed_pages=0,
                    error=str(e),
                )

        raise ValueError("No multimodal processor available")

    def supports_mode(self, mode: ProcessingMode) -> bool:
        """Check if a processing mode is supported."""
        if mode == ProcessingMode.SCANNED:
            return self.vision_processor is not None
        elif mode == ProcessingMode.TEXT_ONLY or mode == ProcessingMode.MULTIMODAL:
            return self.knowledge_service is not None
        return False
