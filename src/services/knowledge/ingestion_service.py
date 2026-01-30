"""Ingestion service for knowledge base.

This service handles document processing, chunking, embedding, and indexing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from ...config.settings import Settings
from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .chunking import ChunkingConfig, process_document, flatten_chunks, merge_small_chunks, Chunk
from .embedding import create_embedding, BaseEmbedding, get_cached_embedder
from .vector_store import VectorStore

logger = get_logger(__name__)


def _ensure_dict(value: Any) -> Dict[str, Any]:
    """Ensure value is a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class IngestionService:
    """Service for ingesting documents into knowledge base."""

    def __init__(
        self,
        settings: Settings,
        database: DatabaseStorage,
        vector_store: VectorStore,
    ):
        self.settings = settings
        self.db = database
        self.vector_store = vector_store

    # ========================================================================
    # Main Ingestion Pipeline
    # ========================================================================

    async def ingest_document(
        self,
        dataset_id: str,
        document_id: str,
        text: str,
        doc_metadata: Optional[Dict[str, Any]] = None,
        chunking_config: Optional[ChunkingConfig] = None,
        embedding_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ingest a document into the knowledge base.
        
        Pipeline:
        1. Segment text into chunks
        2. Generate embeddings
        3. Store in vector database
        4. Update document status
        """
        doc_metadata = doc_metadata or {}
        doc_name = doc_metadata.get("title", document_id)
        
        # Step 1: Update status to parsing
        await self.db.update_document_status(document_id, status="parsing", progress=10)
        
        text = text.strip()
        if not text:
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error="empty document"
            )
            return {"success": False, "error": "empty document"}

        # Step 2: Segmenting
        await self.db.update_document_status(document_id, status="segmenting", progress=25)
        
        chunks = await self._create_chunks(text, chunking_config, doc_name, document_id, dataset_id)
        
        if not chunks:
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error="no segments generated"
            )
            return {"success": False, "error": "no segments generated"}

        # Step 3: Embedding
        await self.db.update_document_status(document_id, status="embedding", progress=50)
        
        try:
            await self._embed_and_store_chunks(
                dataset_id=dataset_id,
                document_id=document_id,
                chunks=chunks,
                embedding_config=embedding_config,
            )
        except asyncio.TimeoutError as e:
            logger.error(f"Embedding timeout for document {document_id}: {e}")
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error="embedding timeout"
            )
            return {"success": False, "error": "embedding timeout"}
        except ConnectionError as e:
            logger.error(f"Embedding service connection error for document {document_id}: {e}")
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error="embedding service unavailable"
            )
            return {"success": False, "error": "embedding service unavailable"}
        except ValueError as e:
            logger.error(f"Embedding validation error for document {document_id}: {e}")
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error=f"embedding error: {e}"
            )
            return {"success": False, "error": str(e)}
        except RuntimeError as e:
            logger.error(f"Embedding runtime error for document {document_id}: {e}")
            await self.db.update_document_status(
                document_id, status="failed", progress=100, error=f"embedding error: {e}"
            )
            return {"success": False, "error": str(e)}

        # Step 4: Complete
        await self.db.update_document_status(document_id, status="completed", progress=100)
        
        logger.info(f"Document {document_id} ingested successfully with {len(chunks)} chunks")
        
        return {
            "success": True,
            "document_id": document_id,
            "chunk_count": len(chunks),
        }

    async def reindex_document(
        self,
        dataset_id: str,
        document_id: str,
        new_text: str,
        chunking_config: Optional[ChunkingConfig] = None,
    ) -> Dict[str, Any]:
        """Reindex a document with new content (incremental update)."""
        # Delete old segments
        old_segments = await self.db.get_segments_by_document(document_id)
        
        # Delete old vectors (batch delete)
        vector_ids = [seg.get("vector_id") for seg in old_segments if seg.get("vector_id")]
        if vector_ids:
            try:
                await self.vector_store.delete_points(dataset_id, vector_ids)
            except ConnectionError as e:
                logger.warning(f"Vector store connection error deleting vectors: {e}")
            except KeyError:
                logger.debug(f"Vectors not found (may have been deleted)")
            except RuntimeError as e:
                logger.warning(f"Vector store error deleting vectors: {e}")
        
        # Delete old segments from DB
        await self.db.delete_segments_by_document(document_id)
        
        # Re-ingest
        return await self.ingest_document(
            dataset_id=dataset_id,
            document_id=document_id,
            text=new_text,
            chunking_config=chunking_config,
        )

    # ========================================================================
    # Chunking
    # ========================================================================

    async def _create_chunks(
        self,
        text: str,
        config: Optional[ChunkingConfig],
        doc_name: str,
        document_id: str,
        dataset_id: str,
    ) -> List[Tuple[str, int, str, Dict[str, Any]]]:
        """Create chunks from text.
        
        Returns list of (text, token_count, content_hash, metadata) tuples.
        """
        config = config or ChunkingConfig()
        
        # Process document
        chunk_objects = process_document(text, config, document_id)
        logger.info(f"Generated {len(chunk_objects)} chunks for document {document_id}")
        
        # Flatten hierarchical chunks
        flat_chunks = flatten_chunks(chunk_objects)
        
        # Merge undersized chunks
        flat_chunks = merge_small_chunks(
            flat_chunks,
            min_size=config.min_chunk_size,
            max_size=config.max_chunk_size,
        )
        
        # Add source metadata
        for c in flat_chunks:
            c.metadata["source_document"] = doc_name
            c.metadata["source_document_id"] = document_id
            c.metadata["source_dataset_id"] = dataset_id
        
        # Convert to tuple format with content hash
        chunks = []
        for c in flat_chunks:
            content_hash = hashlib.sha256(c.text.encode()).hexdigest()
            chunks.append((c.text, c.token_count, content_hash, c.metadata))
        
        return chunks

    # ========================================================================
    # Embedding and Storage
    # ========================================================================

    async def _embed_and_store_chunks(
        self,
        dataset_id: str,
        document_id: str,
        chunks: List[Tuple[str, int, str, Dict[str, Any]]],
        embedding_config: Optional[Dict[str, Any]],
    ) -> None:
        """Embed chunks and store in vector database."""
        config = embedding_config or {}
        provider = config.get("provider", "local")
        model = config.get("model", "hash-384")
        
        # Create embedder
        embedder = create_embedding(config)
        
        # Ensure collection exists
        if embedder._dimension is None:
            await embedder.embed_query("test")
        
        dim = embedder._dimension or 1024
        await self.vector_store.ensure_collection(
            dataset_id=dataset_id,
            dimension=dim,
        )
        
        # Process in batches
        batch_size = 32
        total_inserted = 0
        
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            
            # Generate embeddings
            texts = [c[0] for c in batch]
            embeddings = await embedder.embed_documents(texts)
            
            # Prepare segments for DB
            segments = []
            for j, (text, token_count, content_hash, meta) in enumerate(batch):
                segment_id = f"{document_id}_{i + j}"
                segments.append({
                    "segment_id": segment_id,
                    "document_id": document_id,
                    "dataset_id": dataset_id,
                    "position": i + j,
                    "text": text,
                    "token_count": token_count,
                    "content_hash": content_hash,
                    "metadata": meta,
                })
            
            # Insert to DB
            await self.db.insert_segments(segments)
            
            # Insert to vector store
            points = []
            for j, seg in enumerate(segments):
                points.append({
                    "id": seg["segment_id"],
                    "vector": embeddings[j],
                    "payload": {
                        "document_id": document_id,
                        "dataset_id": dataset_id,
                        "text": seg["content"][:1000],  # Truncate for payload
                    },
                })
            
            await self.vector_store.upsert(dataset_id, points)
            total_inserted += len(batch)
            
            # Update progress
            progress = 50 + int((total_inserted / len(chunks)) * 40)
            await self.db.update_document_status(document_id, status="embedding", progress=progress)

    # ========================================================================
    # Incremental Update Support
    # ========================================================================

    async def compute_content_hashes(
        self,
        chunks: List[Tuple[str, int, str, Dict[str, Any]]],
    ) -> Dict[int, str]:
        """Compute content hashes for chunks by position."""
        return {i: chunk[2] for i, chunk in enumerate(chunks)}

    async def get_changed_chunks(
        self,
        document_id: str,
        new_chunks: List[Tuple[str, int, str, Dict[str, Any]]],
    ) -> Tuple[List[int], List[int], List[int]]:
        """Compare new chunks with existing to find changes.
        
        Returns:
            (unchanged_positions, changed_positions, excess_positions)
        """
        existing = await self.db.get_segment_hashes_by_document(document_id)
        
        unchanged = []
        changed = []
        
        for pos, (_, _, new_hash, _) in enumerate(new_chunks):
            old = existing.get(pos)
            if old and old.get("content_hash") == new_hash:
                unchanged.append(pos)
            else:
                changed.append(pos)
        
        # Find excess old positions
        max_new_pos = len(new_chunks) - 1
        excess = [pos for pos in existing.keys() if pos > max_new_pos]
        
        return unchanged, changed, excess
