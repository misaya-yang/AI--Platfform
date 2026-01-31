"""
Processor Factory - Unified document processor creation

Creates and configures document processors based on processing mode:
- text_only: OCR + text embedding
- scanned: Page-as-image vision embedding
- multimodal: Combined text + image embedding

Integrates with the hierarchical indexing and streaming loader systems.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Tuple, TYPE_CHECKING

from ...core.observability.logging import get_logger
from .processing_mode import ProcessingMode

logger = get_logger(__name__)

# FIX: Forward references to avoid circular imports
# These are resolved at runtime when needed
BatchResult = Any
PageContent = Any


@dataclass
class ProcessedChunk:
    """A processed chunk ready for indexing."""
    
    text: str
    vector: Optional[List[float]] = None
    page_number: Optional[int] = None
    position: int = 0
    content_type: str = "text"  # text | image | page_image
    image_bytes: Optional[bytes] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class ProcessingContext:
    """Context for document processing."""
    
    document_id: str
    dataset_id: str
    filename: str
    mime_type: str
    file_size: int
    chunking_config: Optional[Any] = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class DocumentProcessor(ABC):
    """Abstract base class for document processors."""
    
    @abstractmethod
    async def process_batch(
        self,
        batch: "Any",  # Forward ref: BatchResult
        context: ProcessingContext,
    ) -> List[ProcessedChunk]:
        """Process a batch of pages."""
        pass
    
    @abstractmethod
    async def finalize(
        self,
        context: ProcessingContext,
        all_chunks: List[ProcessedChunk],
    ) -> List[ProcessedChunk]:
        """Finalize processing after all batches."""
        pass


class TextProcessor(DocumentProcessor):
    """
    Text-only document processor.
    
    Extracts text from pages and creates text chunks.
    Uses OCR if needed, then applies chunking strategy.
    """
    
    def __init__(
        self,
        embedder: Any,
        chunking_config: Optional[Any] = None,
        ocr_service: Optional[Any] = None,
    ):
        """
        Initialize text processor.
        
        Args:
            embedder: Text embedding service
            chunking_config: Chunking configuration
            ocr_service: Optional OCR service for scanned pages
        """
        self.embedder = embedder
        self.chunking_config = chunking_config
        self.ocr_service = ocr_service
    
    async def process_batch(
        self,
        batch: "Any",  # Forward ref: BatchResult
        context: ProcessingContext,
    ) -> List[ProcessedChunk]:
        """Extract and chunk text from pages."""
        chunks = []
        
        # Combine text from all pages in batch
        combined_text = ""
        for page in batch.pages:
            if page.text.strip():
                combined_text += f"\n[Page {page.page_number}]\n{page.text}"
            elif self.ocr_service and page.images:
                # Apply OCR to page images
                for img in page.images:
                    try:
                        ocr_text = await self.ocr_service.extract_text(img)
                        if ocr_text:
                            combined_text += f"\n[Page {page.page_number}]\n{ocr_text}"
                    except Exception as e:
                        logger.debug(f"OCR failed for page {page.page_number}: {e}")
        
        if not combined_text.strip():
            return []
        
        # Apply chunking
        from .chunking import create_chunker, ChunkingConfig
        
        config = context.chunking_config or self.chunking_config or ChunkingConfig()
        chunker = create_chunker(config)
        text_chunks = chunker.chunk(combined_text)
        
        # Create processed chunks
        for i, chunk in enumerate(text_chunks):
            chunks.append(ProcessedChunk(
                text=chunk.text,
                position=i,
                page_number=chunk.metadata.get("page"),
                content_type="text",
                metadata={
                    "batch_index": batch.batch_index,
                    "token_count": chunk.token_count,
                    **chunk.metadata,
                },
            ))
        
        return chunks
    
    async def finalize(
        self,
        context: ProcessingContext,
        all_chunks: List[ProcessedChunk],
    ) -> List[ProcessedChunk]:
        """Generate embeddings for all chunks."""
        if not all_chunks:
            return []
        
        # Batch embed all texts
        texts = [c.text for c in all_chunks]
        
        try:
            vectors = await self.embedder.embed_documents(texts)
            
            for chunk, vector in zip(all_chunks, vectors):
                chunk.vector = vector
                
        except Exception as e:
            logger.error(f"Text embedding failed: {e}")
        
        return all_chunks


class ScannedProcessor(DocumentProcessor):
    """
    Scanned document processor (Vision-first).
    
    Treats each page as an image and embeds using vision model.
    No OCR - purely visual embedding.
    """
    
    def __init__(
        self,
        multimodal_embedder: Any,
        dpi: int = 150,
    ):
        """
        Initialize scanned processor.
        
        Args:
            multimodal_embedder: Vision embedding service
            dpi: Resolution for page rendering
        """
        self.embedder = multimodal_embedder
        self.dpi = dpi
    
    async def process_batch(
        self,
        batch: "Any",  # Forward ref: BatchResult
        context: ProcessingContext,
    ) -> List[ProcessedChunk]:
        """Convert pages to images and embed."""
        chunks = []
        
        # Collect page images
        page_images = []
        for page in batch.pages:
            if page.images:
                # Use first extracted image as page representation
                page_images.append((page.page_number, page.images[0]))
            else:
                # Page has no image, skip
                continue
        
        if not page_images:
            return []
        
        # Batch embed images
        try:
            image_bytes_list = [img for _, img in page_images]
            vectors = await self.embedder.embed_images(image_bytes_list)
            
            for (page_num, img_bytes), vector in zip(page_images, vectors):
                if vector:
                    chunks.append(ProcessedChunk(
                        text=f"[Page {page_num}]",
                        vector=vector,
                        page_number=page_num,
                        position=page_num,
                        content_type="page_image",
                        image_bytes=img_bytes,
                        metadata={
                            "processing_mode": "scanned",
                            "batch_index": batch.batch_index,
                        },
                    ))
                    
        except Exception as e:
            logger.error(f"Image embedding failed: {e}")
        
        return chunks
    
    async def finalize(
        self,
        context: ProcessingContext,
        all_chunks: List[ProcessedChunk],
    ) -> List[ProcessedChunk]:
        """No additional finalization needed for scanned mode."""
        return all_chunks


class MultimodalProcessor(DocumentProcessor):
    """
    Multimodal document processor.
    
    Processes both text and images from documents.
    Creates separate chunks for text and image content.
    """
    
    def __init__(
        self,
        text_embedder: Any,
        multimodal_embedder: Any,
        chunking_config: Optional[Any] = None,
    ):
        """
        Initialize multimodal processor.
        
        Args:
            text_embedder: Text embedding service
            multimodal_embedder: Vision embedding service
            chunking_config: Chunking configuration
        """
        self.text_embedder = text_embedder
        self.image_embedder = multimodal_embedder
        self.chunking_config = chunking_config
    
    async def process_batch(
        self,
        batch: "Any",  # Forward ref: BatchResult
        context: ProcessingContext,
    ) -> List[ProcessedChunk]:
        """Process both text and images from pages."""
        text_chunks = []
        image_chunks = []
        
        # Process text
        combined_text = ""
        for page in batch.pages:
            if page.text.strip():
                combined_text += f"\n[Page {page.page_number}]\n{page.text}"
        
        if combined_text.strip():
            from .chunking import create_chunker, ChunkingConfig
            
            config = context.chunking_config or self.chunking_config or ChunkingConfig()
            chunker = create_chunker(config)
            chunks = chunker.chunk(combined_text)
            
            for i, chunk in enumerate(chunks):
                text_chunks.append(ProcessedChunk(
                    text=chunk.text,
                    position=len(text_chunks),
                    page_number=chunk.metadata.get("page"),
                    content_type="text",
                    metadata={"batch_index": batch.batch_index},
                ))
        
        # Process images
        for page in batch.pages:
            for idx, img_bytes in enumerate(page.images):
                image_chunks.append(ProcessedChunk(
                    text=f"[Image from page {page.page_number}]",
                    page_number=page.page_number,
                    position=len(image_chunks),
                    content_type="image",
                    image_bytes=img_bytes,
                    metadata={
                        "image_index": idx,
                        "batch_index": batch.batch_index,
                    },
                ))
        
        return text_chunks + image_chunks
    
    async def finalize(
        self,
        context: ProcessingContext,
        all_chunks: List[ProcessedChunk],
    ) -> List[ProcessedChunk]:
        """Generate embeddings for text and image chunks."""
        if not all_chunks:
            return []
        
        # Separate text and image chunks
        text_chunks = [c for c in all_chunks if c.content_type == "text"]
        image_chunks = [c for c in all_chunks if c.content_type in ("image", "page_image")]
        
        # Embed text chunks
        if text_chunks:
            texts = [c.text for c in text_chunks]
            try:
                vectors = await self.text_embedder.embed_documents(texts)
                for chunk, vector in zip(text_chunks, vectors):
                    chunk.vector = vector
            except Exception as e:
                logger.error(f"Text embedding failed: {e}")
        
        # Embed image chunks
        if image_chunks and self.image_embedder:
            images = [c.image_bytes for c in image_chunks if c.image_bytes]
            if images:
                try:
                    vectors = await self.image_embedder.embed_images(images)
                    vec_idx = 0
                    for chunk in image_chunks:
                        if chunk.image_bytes:
                            chunk.vector = vectors[vec_idx] if vec_idx < len(vectors) else None
                            vec_idx += 1
                except Exception as e:
                    logger.error(f"Image embedding failed: {e}")
        
        return all_chunks


class ProcessorFactory:
    """
    Factory for creating document processors.
    
    Creates the appropriate processor based on processing mode.
    """
    
    def __init__(
        self,
        text_embedder: Any,
        multimodal_embedder: Optional[Any] = None,
        ocr_service: Optional[Any] = None,
        default_chunking_config: Optional[Any] = None,
    ):
        """
        Initialize factory with embedding services.
        
        Args:
            text_embedder: Text embedding service
            multimodal_embedder: Optional vision embedding service
            ocr_service: Optional OCR service
            default_chunking_config: Default chunking configuration
        """
        self.text_embedder = text_embedder
        self.multimodal_embedder = multimodal_embedder
        self.ocr_service = ocr_service
        self.default_chunking_config = default_chunking_config
    
    def create(self, mode: ProcessingMode) -> DocumentProcessor:
        """
        Create a processor for the given mode.
        
        Args:
            mode: Processing mode
            
        Returns:
            Configured document processor
        """
        if mode == ProcessingMode.SCANNED:
            if not self.multimodal_embedder:
                raise ValueError("Multimodal embedder required for scanned mode")
            return ScannedProcessor(self.multimodal_embedder)
        
        elif mode == ProcessingMode.MULTIMODAL:
            return MultimodalProcessor(
                text_embedder=self.text_embedder,
                multimodal_embedder=self.multimodal_embedder,
                chunking_config=self.default_chunking_config,
            )
        
        else:  # TEXT_ONLY
            return TextProcessor(
                embedder=self.text_embedder,
                chunking_config=self.default_chunking_config,
                ocr_service=self.ocr_service,
            )
    
    def get_recommended_processor(
        self,
        detection_result: Any,
    ) -> DocumentProcessor:
        """
        Get processor based on detection result.
        
        Args:
            detection_result: Result from DocumentTypeDetector
            
        Returns:
            Configured document processor
        """
        mode = detection_result.recommended_mode
        return self.create(mode)
