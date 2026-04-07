"""Ingestion service for knowledge base.

This service handles document processing, chunking, embedding, and indexing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ...config.settings import Settings
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .chunking import (
    Chunk,
    ChunkingConfig,
    ChunkingMode,
    enforce_token_limits,
    flatten_chunks,
    get_token_counter,
    process_document,
)
from .embedding import create_embedding
from .retrieval import text_to_sparse_vector
from .section_extractor import SectionExtractor
from .vector_store import VectorStore

logger = get_logger(__name__)


def _ensure_dict(value: Any) -> dict[str, Any]:
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
        doc_metadata: dict[str, Any] | None = None,
        chunking_config: ChunkingConfig | None = None,
        embedding_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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

        # Step 2.5: LLM metadata extraction (optional, non-blocking)
        if self.settings.metadata_llm.enabled and self.settings.metadata_llm.api_key:
            try:
                chunks = await self._enrich_chunks_with_llm(chunks)
                logger.info(
                    f"[Ingest] LLM metadata extracted for {len(chunks)} chunks "
                    f"(doc={document_id})"
                )
            except Exception as llm_err:
                logger.warning(f"[Ingest] LLM metadata extraction failed (non-fatal): {llm_err}")

        # Step 3: Embedding
        await self.db.update_document_status(document_id, status="embedding", progress=50)

        try:
            failed_batches = await self._embed_and_store_chunks(
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

        # Step 4: Complete — distinguish full vs partial success
        if failed_batches > 0:
            await self.db.update_document_status(
                document_id, status="partial", progress=100,
                error=f"{failed_batches} embedding batch(es) failed — document partially indexed",
            )
            logger.warning(
                "Document %s partially ingested: %d/%d chunks, %d batches failed",
                document_id, len(chunks), len(chunks), failed_batches,
            )
        else:
            await self.db.update_document_status(document_id, status="completed", progress=100)
            logger.info(f"Document {document_id} ingested successfully with {len(chunks)} chunks")

        return {
            "success": failed_batches == 0,
            "document_id": document_id,
            "chunk_count": len(chunks),
            "failed_batches": failed_batches,
        }

    async def reindex_document(
        self,
        dataset_id: str,
        document_id: str,
        new_text: str,
        chunking_config: ChunkingConfig | None = None,
    ) -> dict[str, Any]:
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
                logger.debug("Vectors not found (may have been deleted)")
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
        config: ChunkingConfig | None,
        doc_name: str,
        document_id: str,
        dataset_id: str,
    ) -> list[tuple[str, int, str, dict[str, Any]]]:
        """Create chunks from text.

        Returns list of (text, token_count, content_hash, metadata) tuples.
        """
        # CRITICAL FIX: Load chunking config from dataset if not provided
        if config is None:
            config = await self._load_chunking_config(dataset_id)

        # VALIDATION LOG: Log actual config values being used for chunking
        min_tokens_log = config.min_chunk_tokens if config.mode == ChunkingMode.FIXED_SIZE else None
        max_tokens_log = config.max_chunk_tokens if config.mode == ChunkingMode.FIXED_SIZE else None
        logger.info(
            f"[IngestionService._create_chunks] Dataset={dataset_id}, Document={document_id}, "
            f"Mode={config.mode}, "
            f"TokenLimit={config.token_limit}, "
            f"UseTokenCount={config.use_token_count}, "
            f"MinTokens={min_tokens_log}, MaxTokens={max_tokens_log}, "
            f"ParentTokenLimit={config.parent_token_limit}, ChildTokenLimit={config.child_token_limit}, "
            f"ParentSize={config.parent_chunk_size}, ChildSize={config.child_chunk_size}"
        )

        # Process document
        chunk_objects = process_document(text, config, document_id)
        logger.info(f"Generated {len(chunk_objects)} chunks for document {document_id}")

        # Flatten hierarchical chunks
        flat_chunks = flatten_chunks(chunk_objects)

        # Apply strict sizing only for fixed-size mode; other modes follow their own logic
        if config.mode == ChunkingMode.FIXED_SIZE and config.use_token_count:
            token_limit = int(config.token_limit or 0)
            if token_limit > 0:
                flat_chunks = enforce_token_limits(
                    flat_chunks,
                    token_limit,
                    min_tokens=None,
                )

        # Apply strict section traceability if enabled
        if config.strict_section_traceability:
            flat_chunks = self._apply_section_traceability(text, flat_chunks, doc_name)

        # Add source metadata
        for i, c in enumerate(flat_chunks):
            c.index = i
            c.metadata["chunk_index"] = i
            c.metadata["paragraph_index"] = i
            c.metadata["source_document"] = doc_name
            c.metadata["source_document_id"] = document_id
            c.metadata["source_dataset_id"] = dataset_id

        # Convert to tuple format with content hash
        chunks = []
        for c in flat_chunks:
            content_hash = hashlib.sha256(c.text.encode()).hexdigest()
            chunks.append((c.text, c.token_count, content_hash, c.metadata))

        return chunks

    def _apply_section_traceability(
        self,
        text: str,
        chunks: list[Chunk],
        doc_name: str,
    ) -> list[Chunk]:
        """Apply strict section traceability to chunks.

        Ensures every chunk has a section_title and includes it in citations.
        """
        extractor = SectionExtractor()

        # Get section metadata for all chunks
        section_metadata_list = extractor.assign_section_to_chunks(text, chunks)

        # Apply section metadata to chunks
        for i, (chunk, section_metadata) in enumerate(
            zip(chunks, section_metadata_list, strict=False)
        ):
            # Ensure section_title exists
            if not section_metadata.get("section_title"):
                # Force section title if missing
                forced = extractor.force_section_title(
                    [chunk], document_title=doc_name, default_section="Main Content"
                )
                if forced:
                    section_metadata.update(forced[0])

            # Update chunk metadata
            chunk.metadata.update(section_metadata)

            # Build section-aware citation if not already present
            if not chunk.metadata.get("citation_text"):
                from .section_extractor import get_section_aware_citation

                citation = get_section_aware_citation(
                    chunk.metadata, document_name=doc_name, position=i
                )
                chunk.metadata["citation_text"] = citation

        return chunks

    async def _load_chunking_config(self, dataset_id: str) -> ChunkingConfig:
        """Load chunking config from dataset with proper fallback."""
        try:
            dataset = await self.db.get_dataset(dataset_id)
            if dataset:
                index_config = dataset.get("index_config") or {}
                if isinstance(index_config, str):
                    try:
                        index_config = json.loads(index_config)
                    except json.JSONDecodeError:
                        index_config = {}
                chunking_dict = (
                    index_config.get("chunking") if isinstance(index_config, dict) else {}
                )
                if chunking_dict:
                    logger.info(
                        f"[Chunking] Loaded config from dataset {dataset_id}: {chunking_dict}"
                    )
                    return ChunkingConfig.from_dict(chunking_dict)
        except Exception as e:
            logger.warning(f"[Chunking] Failed to load config for {dataset_id}: {e}")

        # Fallback to default
        logger.info(f"[Chunking] Using default config for dataset {dataset_id}")
        return ChunkingConfig()

    def _validate_chunk_sizes(self, chunks: list[Chunk], config: ChunkingConfig) -> list[Chunk]:
        """Validate and enforce strict token limits on chunks.

        This is a safety pass that splits any chunks exceeding max_tokens.
        """
        if not config.use_token_count:
            return chunks
        max_tokens = config.max_chunk_tokens if config.max_chunk_tokens is not None else None
        if max_tokens is None:
            return chunks

        validated = []
        for chunk in chunks:
            chunk_tokens = chunk.token_count

            if chunk_tokens > max_tokens:
                # Split oversized chunk
                logger.warning(
                    f"[Chunking] Chunk {chunk.index} exceeds limit: "
                    f"{chunk_tokens} > {max_tokens}, splitting..."
                )
                split_chunks = self._split_chunk_strict(chunk, max_tokens)
                validated.extend(split_chunks)
            elif config.min_chunk_tokens is not None and chunk_tokens < config.min_chunk_tokens:
                # Mark as small for potential merging
                chunk.metadata["_too_small"] = True
                validated.append(chunk)
            else:
                validated.append(chunk)

        return validated

    def _split_chunk_strict(self, chunk: Chunk, max_tokens: int) -> list[Chunk]:
        """Split a chunk strictly by token count."""
        words = chunk.text.split()
        if not words:
            return [chunk]

        token_counter = get_token_counter()
        result = []
        current_words = []
        current_tokens = 0
        index = 0

        for word in words:
            word_tokens = token_counter.count_tokens(word + " ")
            if current_tokens + word_tokens > max_tokens and current_words:
                # Create chunk from current buffer
                text = " ".join(current_words)
                result.append(
                    Chunk(
                        text=text,
                        index=chunk.index + index,
                        metadata={**chunk.metadata, "_split_from": chunk.hash_id},
                        parent_id=chunk.parent_id,
                    )
                )
                index += 1
                current_words = [word]
                current_tokens = word_tokens
            else:
                current_words.append(word)
                current_tokens += word_tokens

        # Don't forget remaining words
        if current_words:
            text = " ".join(current_words)
            result.append(
                Chunk(
                    text=text,
                    index=chunk.index + index,
                    metadata={**chunk.metadata, "_split_from": chunk.hash_id},
                    parent_id=chunk.parent_id,
                )
            )

        return result if result else [chunk]

    # ========================================================================
    # LLM Metadata Enrichment (P1)
    # ========================================================================

    async def _enrich_chunks_with_llm(
        self,
        chunks: list[tuple[str, int, str, dict[str, Any]]],
    ) -> list[tuple[str, int, str, dict[str, Any]]]:
        """Enrich chunk metadata using LLM extraction (Qwen 3.6-Plus).

        Non-destructive: only fills in topic/entities/keywords/summary if
        not already present in the chunk metadata.
        """
        from .metadata_extractor import MetadataExtractor

        cfg = self.settings.metadata_llm
        extractor = MetadataExtractor(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            provider=cfg.provider,
            max_concurrent=cfg.max_concurrent,
            timeout=cfg.timeout_seconds,
        )

        try:
            texts = [c[0] for c in chunks]
            results = await extractor.extract_batch(texts, batch_size=cfg.batch_size)

            enriched = []
            success_count = 0
            for (text, token_count, content_hash, meta), result in zip(chunks, results):
                if result.success:
                    success_count += 1
                    updated_meta = {**meta}
                    # Only fill gaps — don't override existing metadata
                    if not updated_meta.get("topic"):
                        updated_meta["topic"] = result.topic
                    if not updated_meta.get("entities"):
                        updated_meta["entities"] = result.entities
                    if not updated_meta.get("keywords"):
                        updated_meta["keywords"] = result.keywords
                    if not updated_meta.get("summary"):
                        updated_meta["summary"] = result.summary
                    enriched.append((text, token_count, content_hash, updated_meta))
                else:
                    enriched.append((text, token_count, content_hash, meta))

            logger.info(f"[LLM Metadata] Enriched {success_count}/{len(chunks)} chunks")
            return enriched
        finally:
            await extractor.close()

    # ========================================================================
    # Embedding and Storage
    # ========================================================================

    async def _embed_and_store_chunks(
        self,
        dataset_id: str,
        document_id: str,
        chunks: list[tuple[str, int, str, dict[str, Any]]],
        embedding_config: dict[str, Any] | None,
    ) -> int:
        """Embed chunks and store in vector database.

        Returns:
            Number of failed embedding batches (0 = all succeeded).
        """
        config = embedding_config or {}
        config.get("provider", "local")
        config.get("model", "hash-384")

        # Create embedder
        embedder = create_embedding(config)

        # Ensure collection exists
        if embedder._dimension is None:
            await embedder.embed_query("test")

        dim = embedder._dimension or 1024  # fallback dimension for unknown embedders
        await self.vector_store.ensure_collection(
            dataset_id=dataset_id,
            dimension=dim,
        )

        # Process in smaller batches to avoid Qdrant timeout
        # For large documents, use smaller batch size to prevent timeout
        total_chunks = len(chunks)
        # Adaptive batch size: smaller batches for large document counts
        if total_chunks > 200:
            batch_size = 16
        elif total_chunks > 100:
            batch_size = 24
        else:
            batch_size = 32

        # Further reduce batch size for Qdrant upsert to avoid timeout
        qdrant_batch_size = 16

        total_inserted = 0
        failed_batches = 0

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]

            # Generate embeddings
            texts = [c[0] for c in batch]
            try:
                embeddings = await embedder.embed_documents(texts)
            except Exception as embed_err:
                failed_batches += 1
                logger.error(f"Embedding failed for batch {i // batch_size + 1}: {embed_err}")
                await self.db.update_document_status(
                    document_id,
                    status="embedding",
                    progress=50 + int((total_inserted / len(chunks)) * 40),
                    error=f"Embedding failed for batch {i // batch_size + 1}: {str(embed_err)[:200]}",
                )
                continue

            # Prepare segments for DB
            segments = []
            for j, (text, token_count, content_hash, meta) in enumerate(batch):
                segment_id = f"{document_id}_{i + j}"
                segments.append(
                    {
                        "segment_id": segment_id,
                        "document_id": document_id,
                        "dataset_id": dataset_id,
                        "position": i + j,
                        "text": text,
                        "token_count": token_count,
                        "content_hash": content_hash,
                        "metadata": meta,
                    }
                )

            # Insert to DB first (more reliable)
            try:
                await self.db.insert_segments(segments)
            except Exception as db_err:
                logger.error(f"Database insert failed for batch {i // batch_size + 1}: {db_err}")
                # Continue - segments can be re-inserted on retry

            # Insert to vector store in smaller sub-batches
            from qdrant_client.http import models as qmodels

            points = []
            for j, seg in enumerate(segments):
                if j < len(embeddings):  # Safety check
                    meta = seg.get("metadata") or {}
                    payload_meta = {
                        key: meta.get(key)
                        for key in (
                            "source_type",
                            "citation_text",
                            "source_reference",
                            "section_title",
                            "section_full_path",
                            "page_number",
                            "chunk_index",
                            "paragraph_index",
                            "source_document",
                            "document_title",
                            "madhab",
                            "language",
                            # P1: Generic LLM metadata fields
                            "topic",
                            "entities",
                            "keywords",
                            "summary",
                        )
                        if meta.get(key) is not None
                    }
                    # P2a: Generate BM25 sparse vector for native Qdrant search
                    sparse_indices, sparse_values = text_to_sparse_vector(seg["text"])
                    vector: dict | list = (
                        {
                            "": embeddings[j],
                            "bm25": qmodels.SparseVector(
                                indices=sparse_indices, values=sparse_values,
                            ),
                        }
                        if sparse_indices
                        else embeddings[j]  # Fallback to dense-only if no tokens
                    )
                    points.append(
                        {
                            "id": seg["segment_id"],
                            "vector": vector,
                            "payload": {
                                "document_id": document_id,
                                "dataset_id": dataset_id,
                                "text": seg["text"][:1000],  # Truncate for payload
                                "metadata": payload_meta,
                                "source_type": payload_meta.get("source_type"),
                                "citation_text": payload_meta.get("citation_text"),
                                "source_reference": payload_meta.get("source_reference"),
                            },
                        }
                    )

            # Upsert in smaller sub-batches to avoid Qdrant timeout
            for q_start in range(0, len(points), qdrant_batch_size):
                q_batch = points[q_start : q_start + qdrant_batch_size]
                try:
                    await self.vector_store.upsert(dataset_id, q_batch)
                except Exception as qdrant_err:
                    logger.error(
                        f"Qdrant upsert failed for sub-batch {q_start // qdrant_batch_size + 1} "
                        f"in batch {i // batch_size + 1}: {qdrant_err}"
                    )
                    # Continue with other batches - failed segments can be recovered
                    # by reindexing the document later

            total_inserted += len(batch)

            # Update progress
            progress = 50 + int((total_inserted / len(chunks)) * 40)
            await self.db.update_document_status(document_id, status="embedding", progress=progress)

        return failed_batches

    # ========================================================================
    # Incremental Update Support
    # ========================================================================

    async def compute_content_hashes(
        self,
        chunks: list[tuple[str, int, str, dict[str, Any]]],
    ) -> dict[int, str]:
        """Compute content hashes for chunks by position."""
        return {i: chunk[2] for i, chunk in enumerate(chunks)}

    async def get_changed_chunks(
        self,
        document_id: str,
        new_chunks: list[tuple[str, int, str, dict[str, Any]]],
    ) -> tuple[list[int], list[int], list[int]]:
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
        excess = [pos for pos in existing if pos > max_new_pos]

        return unchanged, changed, excess
