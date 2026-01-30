"""
Enhanced Ingestion Pipeline with Structured Document Parsing

Integrates StructuredDocumentParser with the existing knowledge base ingestion flow.

Usage:
    from enhanced_ingestion import EnhancedIngestionPipeline
    
    pipeline = EnhancedIngestionPipeline(
        knowledge_service=kb_service,
        vlm_service=vlm_service,
        use_structured_parsing=True,
    )
    
    result = await pipeline.process_document(
        dataset_id="...",
        document_id="...",
        content_bytes=pdf_bytes,
        filename="document.pdf",
    )
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .structured_document_parser import (
    ChunkType,
    StructuredDocumentParser,
    StructuredChunk,
)
from .chunking import ChunkingConfig, process_document, flatten_chunks

logger = logging.getLogger(__name__)


class EnhancedIngestionPipeline:
    """
    Enhanced document ingestion with structure-aware parsing.
    
    Modes:
    - simple: Original text extraction (fast, for text-heavy docs)
    - structured: Structure-aware parsing (best for mixed docs with images/tables)
    - auto: Automatically choose based on document type
    """
    
    def __init__(
        self,
        knowledge_service: Any,
        vlm_service: Optional[Any] = None,
        use_structured_parsing: bool = True,
        enable_vlm_descriptions: bool = False,
        max_vlm_images_per_doc: int = 10,
        default_mode: str = "auto",
    ):
        self.kb_service = knowledge_service
        self.vlm_service = vlm_service
        self.use_structured_parsing = use_structured_parsing
        self.enable_vlm_descriptions = enable_vlm_descriptions
        self.max_vlm_images = max_vlm_images_per_doc
        self.default_mode = default_mode
        
        # Initialize structured parser
        if use_structured_parsing:
            self.parser = StructuredDocumentParser(
                vlm_service=vlm_service,
                enable_vlm_description=enable_vlm_descriptions,
                max_vlm_images=max_vlm_images_per_doc,
            )
        else:
            self.parser = None
    
    async def process_document(
        self,
        dataset_id: str,
        document_id: str,
        content_bytes: bytes,
        filename: str,
        mime_type: Optional[str] = None,
        mode: Optional[str] = None,  # "simple", "structured", "auto"
    ) -> Dict[str, Any]:
        """
        Process a document through the enhanced pipeline.
        
        Returns metadata about processed chunks.
        """
        mode = mode or self.default_mode
        
        # Determine best mode
        if mode == "auto":
            mode = self._determine_best_mode(filename, mime_type)
        
        logger.info(f"Processing document {document_id} in {mode} mode")
        
        if mode == "structured" and self.parser and filename.lower().endswith('.pdf'):
            return await self._process_structured(
                dataset_id, document_id, content_bytes, filename
            )
        else:
            return await self._process_simple(
                dataset_id, document_id, content_bytes, filename, mime_type
            )
    
    def _determine_best_mode(self, filename: str, mime_type: Optional[str]) -> str:
        """Determine best processing mode based on file type."""
        name_lower = filename.lower()
        
        # PDFs benefit from structured parsing
        if name_lower.endswith('.pdf'):
            return "structured"
        
        # Images benefit from VLM description
        if any(name_lower.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp']):
            return "structured" if self.vlm_service else "simple"
        
        # DOCX can have structure
        if name_lower.endswith('.docx'):
            return "structured"
        
        # Default to simple for text files
        return "simple"
    
    async def _process_structured(
        self,
        dataset_id: str,
        document_id: str,
        content_bytes: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """Process document using structured parsing."""
        if not self.parser:
            raise RuntimeError("Structured parser not initialized")
        
        # Parse document structure
        parse_result = await self.parser.parse_pdf(content_bytes, filename)
        
        logger.info(
            f"Structured parse result: {len(parse_result.chunks)} chunks, "
            f"{len(parse_result.text_chunks())} text, "
            f"{len(parse_result.image_chunks())} images, "
            f"{len(parse_result.table_chunks())} tables"
        )
        
        # Convert structured chunks to knowledge base format
        kb_chunks = []
        for chunk in parse_result.chunks:
            kb_chunk = await self._convert_to_kb_chunk(chunk, dataset_id, document_id)
            if kb_chunk:
                kb_chunks.append(kb_chunk)
        
        # Store chunks
        await self._store_chunks(kb_chunks, dataset_id, document_id)
        
        return {
            "document_id": document_id,
            "total_chunks": len(kb_chunks),
            "text_chunks": len([c for c in kb_chunks if c.get("type") == "text"]),
            "image_chunks": len([c for c in kb_chunks if c.get("type") == "image"]),
            "table_chunks": len([c for c in kb_chunks if c.get("type") == "table"]),
            "vlm_calls": parse_result.document_metadata.get("vlm_calls", 0),
        }
    
    async def _convert_to_kb_chunk(
        self,
        chunk: StructuredChunk,
        dataset_id: str,
        document_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Convert a structured chunk to knowledge base chunk format."""
        
        if chunk.type == ChunkType.TEXT:
            return {
                "type": "text",
                "text": chunk.text,
                "content_for_embedding": chunk.content_for_embedding,
                "page_number": chunk.page_number,
                "metadata": {
                    **chunk.metadata,
                    "section_title": chunk.section_title,
                    "section_level": chunk.section_level,
                }
            }
        
        elif chunk.type == ChunkType.HEADING:
            return {
                "type": "text",
                "text": chunk.text,
                "content_for_embedding": chunk.content_for_embedding,
                "page_number": chunk.page_number,
                "metadata": {
                    **chunk.metadata,
                    "is_heading": True,
                    "heading_level": chunk.section_level,
                }
            }
        
        elif chunk.type == ChunkType.IMAGE:
            # Store image and create reference
            image_data = chunk.images[0] if chunk.images else None
            
            if image_data:
                # Store image to storage if available
                storage_key = None
                if hasattr(self.kb_service, 'image_storage_service') and self.kb_service.image_storage_service:
                    try:
                        storage_key = await self.kb_service.image_storage_service.upload_image(
                            tenant_id=chunk.metadata.get("tenant_id", "default"),
                            document_id=document_id,
                            attachment_id=chunk.chunk_id,
                            filename=f"{chunk.chunk_id}.png",
                            content=image_data["bytes"],
                        )
                    except Exception as e:
                        logger.warning(f"Failed to store image: {e}")
                
                return {
                    "type": "image",
                    "text": chunk.text,  # VLM description or caption
                    "content_for_embedding": chunk.content_for_embedding,
                    "page_number": chunk.page_number,
                    "metadata": {
                        **chunk.metadata,
                        "image_storage_key": storage_key,
                        "image_width": image_data.get("width"),
                        "image_height": image_data.get("height"),
                        "has_vlm_description": bool(chunk.text),
                    }
                }
        
        elif chunk.type == ChunkType.TABLE:
            return {
                "type": "text",  # Tables are stored as text (markdown)
                "text": chunk.text,
                "content_for_embedding": chunk.content_for_embedding,
                "page_number": chunk.page_number,
                "metadata": {
                    **chunk.metadata,
                    "is_table": True,
                    "table_html": chunk.metadata.get("table_html"),
                }
            }
        
        return None
    
    async def _store_chunks(
        self,
        chunks: List[Dict[str, Any]],
        dataset_id: str,
        document_id: str,
    ):
        """Store chunks to database and vector store."""
        if not chunks:
            logger.info(f"No chunks to store for document {document_id}")
            return
        
        logger.info(f"Storing {len(chunks)} chunks for document {document_id}")
        
        # Delegate to knowledge service for actual storage
        # The knowledge service handles:
        # - Database insertion
        # - Embedding generation
        # - Vector store indexing
        try:
            if hasattr(self.kb_service, 'store_document_chunks'):
                await self.kb_service.store_document_chunks(
                    dataset_id=dataset_id,
                    document_id=document_id,
                    chunks=chunks,
                )
            else:
                # Fallback: store chunks one by one using segment creation
                for idx, chunk in enumerate(chunks):
                    await self.kb_service.create_segment(
                        dataset_id=dataset_id,
                        document_id=document_id,
                        segment_index=idx,
                        content=chunk.get("text", ""),
                        metadata=chunk.get("metadata", {}),
                    )
                logger.info(f"Successfully stored {len(chunks)} chunks")
        except Exception as e:
            logger.error(f"Failed to store chunks for document {document_id}: {e}", exc_info=True)
            raise
    
    async def _process_simple(
        self,
        dataset_id: str,
        document_id: str,
        content_bytes: bytes,
        filename: str,
        mime_type: Optional[str],
    ) -> Dict[str, Any]:
        """Process document using simple extraction (fallback)."""
        # Delegate to existing knowledge service
        return await self.kb_service.ingest_document(
            dataset_id=dataset_id,
            document_id=document_id,
        )


# Convenience function for one-off processing
async def process_document_enhanced(
    content_bytes: bytes,
    filename: str,
    vlm_service: Optional[Any] = None,
    enable_vlm: bool = False,
) -> Dict[str, Any]:
    """
    Process a document with enhanced parsing (standalone function).
    
    Example:
        result = await process_document_enhanced(
            pdf_bytes,
            "document.pdf",
            vlm_service=vlm_service,
            enable_vlm=True,
        )
        
        for chunk in result["chunks"]:
            print(f"Type: {chunk['type']}, Content: {chunk['text'][:100]}")
    """
    parser = StructuredDocumentParser(
        vlm_service=vlm_service,
        enable_vlm_description=enable_vlm,
    )
    
    if not filename.lower().endswith('.pdf'):
        raise ValueError("Only PDF files supported in enhanced mode")
    
    parse_result = await parser.parse_pdf(content_bytes, filename)
    
    return {
        "chunks": [
            {
                "type": chunk.type.value,
                "text": chunk.text,
                "content": chunk.content,
                "page_number": chunk.page_number,
                "has_images": chunk.has_images,
                "metadata": chunk.metadata,
            }
            for chunk in parse_result.chunks
        ],
        "metadata": parse_result.document_metadata,
    }
