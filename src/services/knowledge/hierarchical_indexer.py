"""
Hierarchical Indexer - Three-level document indexing

Implements a hierarchical RAG architecture with three levels:
- Level 1 (L1): Document Summary - 1 vector per document for coarse filtering
- Level 2 (L2): Section Chunks - 2000-4000 tokens for medium filtering
- Level 3 (L3): Paragraph Chunks - 300-500 tokens for fine retrieval

Parent-child relationships are maintained for context retrieval.
"""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple

from qdrant_client.http import models as qmodels

logger = logging.getLogger(__name__)


class IndexLevel(IntEnum):
    """Index levels for hierarchical retrieval."""
    DOCUMENT = 1   # L1: Document summary
    SECTION = 2    # L2: Section/chapter
    PARAGRAPH = 3  # L3: Paragraph (current default)


@dataclass
class HierarchicalSegment:
    """A segment with hierarchical metadata."""
    
    segment_id: str
    document_id: str
    dataset_id: str
    level: IndexLevel
    text: str
    summary: Optional[str] = None
    keywords: List[str] = field(default_factory=list)
    position: int = 0
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    vector: Optional[List[float]] = None
    
    @property
    def hash_id(self) -> str:
        """Generate deterministic hash for deduplication."""
        content = f"{self.document_id}:{self.level}:{self.position}:{self.text[:100]}"
        return hashlib.md5(content.encode()).hexdigest()


@dataclass
class IndexingResult:
    """Result of hierarchical indexing."""
    
    document_id: str
    l1_count: int = 0
    l2_count: int = 0
    l3_count: int = 0
    total_vectors: int = 0
    errors: List[str] = field(default_factory=list)
    
    @property
    def success(self) -> bool:
        return len(self.errors) == 0 and self.total_vectors > 0


class HierarchicalIndexer:
    """
    Hierarchical document indexer.
    
    Creates three-level index structure:
    - L1: Document summaries for fast document-level filtering
    - L2: Section chunks for chapter/section level search
    - L3: Paragraph chunks for precise retrieval
    """
    
    # Default chunk sizes (in characters, ~4 chars per token)
    L2_CHUNK_SIZE = 8000   # ~2000 tokens
    L2_CHUNK_OVERLAP = 400
    L3_CHUNK_SIZE = 2000   # ~500 tokens
    L3_CHUNK_OVERLAP = 200
    L2_POSITION_OFFSET = 1_000_000
    
    # Collection name patterns
    SUMMARY_COLLECTION_SUFFIX = "_summary"
    SECTION_COLLECTION_SUFFIX = "_sections"
    
    def __init__(
        self,
        vector_store: Any,
        database: Any,
        embedder: Any,
        summary_generator: Optional[Any] = None,
        levels: List[int] = None,
    ):
        """
        Initialize the hierarchical indexer.
        
        Args:
            vector_store: Qdrant vector store
            database: Database for segment storage
            embedder: Embedding service
            summary_generator: Optional LLM for generating summaries
            levels: Which levels to index [1, 2, 3]. Default: all
        """
        self.vector_store = vector_store
        self.db = database
        self.embedder = embedder
        self.summary_generator = summary_generator
        self.levels = levels or [1, 2, 3]
    
    async def index_document(
        self,
        document_id: str,
        dataset_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        chunking_config: Optional[Any] = None,
        levels_override: Optional[List[int]] = None,
    ) -> IndexingResult:
        """
        Index a document at multiple hierarchy levels.
        
        Args:
            document_id: Document ID
            dataset_id: Dataset ID
            text: Full document text
            metadata: Optional document metadata
            chunking_config: Optional chunking configuration
            
        Returns:
            IndexingResult with counts and status
        """
        result = IndexingResult(document_id=document_id)
        metadata = metadata or {}
        levels = levels_override or self.levels
        
        try:
            # Get embedding dimension
            vector_dim = await self._get_vector_dimension()
            base_collection = await self._resolve_base_collection(dataset_id, vector_dim)

            l2_segments: List[HierarchicalSegment] = []
            l3_segments: List[HierarchicalSegment] = []
            if IndexLevel.SECTION in levels or IndexLevel.PARAGRAPH in levels:
                l2_segments, l3_segments = await self._create_l2_l3_chunks(
                    document_id, dataset_id, text, metadata, chunking_config
                )

            # L3: Paragraph-level chunks
            if IndexLevel.PARAGRAPH in levels:
                await self._index_segments(l3_segments, dataset_id, vector_dim, base_collection)
                result.l3_count = len(l3_segments)
                result.total_vectors += len(l3_segments)

            # L2: Section-level chunks
            if IndexLevel.SECTION in levels:
                await self._index_sections(l2_segments, dataset_id, vector_dim, base_collection)
                result.l2_count = len(l2_segments)
                result.total_vectors += len(l2_segments)
            
            # L1: Document summary
            if IndexLevel.DOCUMENT in levels and self.summary_generator:
                l1_segment = await self._create_l1_summary(
                    document_id, dataset_id, text, metadata
                )
                if l1_segment:
                    await self._index_summary(l1_segment, dataset_id, vector_dim, base_collection)
                    result.l1_count = 1
                    result.total_vectors += 1
            
            logger.info(
                f"[HierarchicalIndexer] Indexed {document_id}: "
                f"L1={result.l1_count}, L2={result.l2_count}, L3={result.l3_count}"
            )
            
        except Exception as e:
            logger.error(f"[HierarchicalIndexer] Failed to index {document_id}: {e}")
            result.errors.append(str(e))
        
        return result
    
    async def _create_l2_l3_chunks(
        self,
        document_id: str,
        dataset_id: str,
        text: str,
        metadata: Dict[str, Any],
        chunking_config: Optional[Any] = None,
    ) -> Tuple[List[HierarchicalSegment], List[HierarchicalSegment]]:
        """Create L2 section and L3 paragraph chunks with parent links."""
        from .chunking import create_chunker, ChunkingConfig, ChunkingMode

        config = ChunkingConfig(
            mode=ChunkingMode.HIERARCHICAL,
            parent_chunk_size=self.L2_CHUNK_SIZE,
            parent_overlap=self.L2_CHUNK_OVERLAP,
            child_chunk_size=self.L3_CHUNK_SIZE,
            child_overlap=self.L3_CHUNK_OVERLAP,
            parent_mode="section",
        )

        if chunking_config:
            config = chunking_config

        chunker = create_chunker(config)
        parents = chunker.chunk(text)

        l2_segments: List[HierarchicalSegment] = []
        l3_segments: List[HierarchicalSegment] = []
        parent_map: Dict[str, str] = {}
        l3_position = 0

        for idx, parent in enumerate(parents):
            segment_id = str(uuid.uuid4())
            parent_map[parent.hash_id] = segment_id

            summary = None
            if self.summary_generator and len(parent.text) > 500:
                try:
                    summary = await self.summary_generator.summarize_section(parent.text)
                except Exception as e:
                    logger.debug(f"Failed to generate section summary: {e}")

            l2_segments.append(
                HierarchicalSegment(
                    segment_id=segment_id,
                    document_id=document_id,
                    dataset_id=dataset_id,
                    level=IndexLevel.SECTION,
                    text=parent.text,
                    summary=summary,
                    position=self.L2_POSITION_OFFSET + idx,
                    metadata={
                        **metadata,
                        "section_index": idx,
                        "heading": parent.metadata.get("heading"),
                        "content_type": "section",
                    },
                )
            )

            for child in parent.children:
                l3_segments.append(
                    HierarchicalSegment(
                        segment_id=str(uuid.uuid4()),
                        document_id=document_id,
                        dataset_id=dataset_id,
                        level=IndexLevel.PARAGRAPH,
                        text=child.text,
                        position=l3_position,
                        page_start=child.metadata.get("page"),
                        page_end=child.metadata.get("page"),
                        parent_id=segment_id,
                        metadata={
                            **metadata,
                            "chunk_index": l3_position,
                            "token_count": child.token_count,
                            "content_type": "text",
                        },
                    )
                )
                l3_position += 1

        return l2_segments, l3_segments
    
    async def _create_l1_summary(
        self,
        document_id: str,
        dataset_id: str,
        text: str,
        metadata: Dict[str, Any],
    ) -> Optional[HierarchicalSegment]:
        """Create L1 document summary."""
        if not self.summary_generator:
            return None
        
        try:
            # Generate document summary and keywords
            summary_result = await self.summary_generator.summarize_document(text)
            
            segment = HierarchicalSegment(
                segment_id=str(uuid.uuid4()),
                document_id=document_id,
                dataset_id=dataset_id,
                level=IndexLevel.DOCUMENT,
                text=summary_result.get("summary", text[:2000]),
                summary=summary_result.get("summary"),
                keywords=summary_result.get("keywords", []),
                position=0,
                metadata={
                    **metadata,
                    "content_type": "document_summary",
                    "topics": summary_result.get("topics", []),
                },
            )
            return segment
            
        except Exception as e:
            logger.error(f"Failed to generate document summary: {e}")
            return None
    
    async def _index_segments(
        self,
        segments: List[HierarchicalSegment],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L3 segments to the main collection."""
        if not segments:
            return

        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            vector_size=vector_dim,
            collection_name=base_collection,
        )
        
        # Generate embeddings
        texts = [s.text for s in segments]
        vectors = await self._embed_texts(texts)
        
        if not vectors or len(vectors) != len(segments):
            raise ValueError("Embedding count mismatch")
        
        # Build points
        points = []
        segment_rows = []
        
        for segment, vector in zip(segments, vectors):
            if vector is None:
                continue
            
            payload = {
                "dataset_id": segment.dataset_id,
                "document_id": segment.document_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
                "level": segment.level,
                "text": segment.text,
                "content_type": "text",
                "parent_segment_id": segment.parent_id,
                **segment.metadata,
            }
            
            points.append(qmodels.PointStruct(
                id=segment.segment_id,
                vector=vector,
                payload=payload,
            ))
            
            segment_rows.append({
                "segment_id": segment.segment_id,
                "dataset_id": segment.dataset_id,
                "document_id": segment.document_id,
                "position": segment.position,
                "level": segment.level,
                "parent_segment_id": segment.parent_id,
                "text": segment.text,
                "token_count": len(segment.text) // 4,
                "vector_id": segment.segment_id,
                "content_type": "text",
                "metadata": segment.metadata,
            })
        
        # Upsert to Qdrant
        if points:
            await self.vector_store.upsert(collection_name=collection, points=points)
        
        # Save to database
        await self.db.insert_segments(segment_rows)
    
    async def _index_sections(
        self,
        segments: List[HierarchicalSegment],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L2 sections to a separate collection."""
        if not segments:
            return

        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            vector_size=vector_dim,
            collection_name=f"{base_collection}{self.SECTION_COLLECTION_SUFFIX}",
        )
        
        # Use summary for embedding if available, otherwise full text
        texts = [s.summary or s.text[:2000] for s in segments]
        vectors = await self._embed_texts(texts)
        
        if not vectors:
            return
        
        points = []
        segment_rows = []
        for segment, vector in zip(segments, vectors):
            if vector is None:
                continue
            
            payload = {
                "dataset_id": segment.dataset_id,
                "document_id": segment.document_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
                "level": segment.level,
                "text": segment.text[:500],  # Truncate for payload
                "summary": segment.summary,
                "content_type": "section",
            }
            
            points.append(qmodels.PointStruct(
                id=segment.segment_id,
                vector=vector,
                payload=payload,
            ))
            segment_rows.append({
                "segment_id": segment.segment_id,
                "dataset_id": segment.dataset_id,
                "document_id": segment.document_id,
                "position": segment.position,
                "level": segment.level,
                "text": segment.text,
                "summary": segment.summary,
                "token_count": len(segment.text) // 4,
                "vector_id": segment.segment_id,
                "content_type": "text",
                "metadata": segment.metadata,
            })
        
        if points:
            await self.vector_store.upsert(collection_name=collection, points=points)
        if segment_rows:
            await self.db.insert_segments(segment_rows)
    
    async def _index_summary(
        self,
        segment: HierarchicalSegment,
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L1 document summary."""
        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            vector_size=vector_dim,
            collection_name=f"{base_collection}{self.SUMMARY_COLLECTION_SUFFIX}",
        )
        
        # Embed the summary
        vectors = await self._embed_texts([segment.summary or segment.text])
        
        if not vectors or not vectors[0]:
            return
        
        payload = {
            "dataset_id": segment.dataset_id,
            "document_id": segment.document_id,
            "segment_id": segment.segment_id,
            "level": segment.level,
            "summary": segment.summary,
            "keywords": segment.keywords,
            "content_type": "document_summary",
        }
        
        point = qmodels.PointStruct(
            id=segment.segment_id,
            vector=vectors[0],
            payload=payload,
        )
        
        await self.vector_store.upsert(collection_name=collection, points=[point])
        
        # Save to document_summaries table
        try:
            await self.db.save_document_summary({
                "document_id": segment.document_id,
                "summary": segment.summary,
                "keywords": segment.keywords,
                "topics": segment.metadata.get("topics", []),
                "vector_id": segment.segment_id,
            })
        except Exception as e:
            logger.debug(f"Failed to save document summary to DB: {e}")
    
    async def _embed_texts(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Generate embeddings for texts."""
        try:
            vectors = await self.embedder.embed_documents(texts)
            return vectors
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return [None] * len(texts)
    
    async def _get_vector_dimension(self) -> int:
        """Get embedding dimension from embedder."""
        try:
            return self.embedder.dimension
        except AttributeError:
            return 1024  # Default for most models
    
    async def _ensure_collection(
        self,
        dataset_id: str,
        vector_size: int,
        collection_name: Optional[str] = None,
    ) -> str:
        """Ensure Qdrant collection exists."""
        try:
            return await self.vector_store.ensure_collection(
                dataset_id=dataset_id,
                dimension=vector_size,
                collection_name=collection_name,
            )
        except Exception as e:
            logger.warning(f"Failed to ensure collection {collection_name}: {e}")
            return collection_name or f"kb_{dataset_id}_{vector_size}"

    async def _resolve_base_collection(self, dataset_id: str, vector_dim: int) -> str:
        """Resolve base collection name for dataset."""
        collection_name = None
        if self.db and hasattr(self.db, "get_dataset"):
            try:
                dataset = await self.db.get_dataset(dataset_id)
                collection_name = str(dataset.get("collection_name") or "") or None
            except Exception:
                collection_name = None
        return await self._ensure_collection(dataset_id, vector_dim, collection_name)
    
    async def delete_document_index(self, document_id: str, dataset_id: str) -> None:
        """Delete all index entries for a document across all levels."""
        vector_dim = await self._get_vector_dimension()
        
        collections = [
            f"kb_{dataset_id}_{vector_dim}",
            f"kb_{dataset_id}_{vector_dim}{self.SECTION_COLLECTION_SUFFIX}",
            f"kb_{dataset_id}_{vector_dim}{self.SUMMARY_COLLECTION_SUFFIX}",
        ]
        
        for collection in collections:
            try:
                await self.vector_store.delete(
                    collection_name=collection,
                    points_selector=qmodels.FilterSelector(
                        filter=qmodels.Filter(
                            must=[
                                qmodels.FieldCondition(
                                    key="document_id",
                                    match=qmodels.MatchValue(value=document_id),
                                )
                            ]
                        )
                    ),
                )
            except Exception as e:
                logger.debug(f"Failed to delete from {collection}: {e}")
