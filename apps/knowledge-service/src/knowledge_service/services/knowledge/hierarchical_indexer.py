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
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from qdrant_client.http import models as qmodels

from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from .chunking import MAX_CHUNK_OUTPUTS
from .lexical_config import LexicalConfig

logger = get_logger(__name__)


class IndexLevel(IntEnum):
    """Index levels for hierarchical retrieval."""

    DOCUMENT = 1  # L1: Document summary
    SECTION = 2  # L2: Section/chapter
    PARAGRAPH = 3  # L3: Paragraph (current default)


@dataclass
class HierarchicalSegment:
    """A segment with hierarchical metadata."""

    segment_id: str
    document_id: str
    dataset_id: str
    level: IndexLevel
    text: str
    summary: str | None = None
    keywords: list[str] = field(default_factory=list)
    position: int = 0
    page_start: int | None = None
    page_end: int | None = None
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    vector: list[float] | None = None

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
    errors: list[str] = field(default_factory=list)

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

    # Default chunk sizes (token-targeted; char size derived at ~4 chars/token)
    DEFAULT_L2_TOKEN_LIMIT = 1500
    DEFAULT_L3_TOKEN_LIMIT = 400
    DEFAULT_L2_CHUNK_SIZE = DEFAULT_L2_TOKEN_LIMIT * 4
    DEFAULT_L2_CHUNK_OVERLAP = 50
    DEFAULT_L3_CHUNK_SIZE = DEFAULT_L3_TOKEN_LIMIT * 4
    DEFAULT_L3_CHUNK_OVERLAP = 50
    L2_POSITION_OFFSET = 1_000_000

    # Collection name patterns
    SUMMARY_COLLECTION_SUFFIX = "_summary"
    SECTION_COLLECTION_SUFFIX = "_sections"

    @classmethod
    def document_collection_names(cls, base_collection: str) -> dict[str, str]:
        """Return the three collection names used by hierarchical indexing."""

        return {
            "base": base_collection,
            "sections": f"{base_collection}{cls.SECTION_COLLECTION_SUFFIX}",
            "summary": f"{base_collection}{cls.SUMMARY_COLLECTION_SUFFIX}",
        }

    @staticmethod
    def document_vector_ids(
        segments: list[dict[str, Any]],
        document_summary: dict[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """Partition persisted vector IDs by their hierarchical collection."""

        partitioned: dict[str, list[str]] = {
            "base": [],
            "sections": [],
            "summary": [],
        }
        seen: dict[str, set[str]] = {name: set() for name in partitioned}

        def append(bucket: str, value: Any) -> None:
            vector_id = str(value or "").strip()
            if vector_id and vector_id not in seen[bucket]:
                seen[bucket].add(vector_id)
                partitioned[bucket].append(vector_id)

        for segment in segments:
            try:
                level = int(segment.get("level") or IndexLevel.PARAGRAPH)
            except (TypeError, ValueError):
                level = int(IndexLevel.PARAGRAPH)
            vector_id = segment.get("vector_id") or segment.get("segment_id")
            if level == int(IndexLevel.SECTION):
                append("sections", vector_id)
            elif level == int(IndexLevel.DOCUMENT):
                append("summary", vector_id)
            else:
                append("base", vector_id)

        if document_summary:
            append("summary", document_summary.get("vector_id"))
        return partitioned

    def __init__(
        self,
        vector_store: Any,
        database: Any,
        embedder: Any,
        summary_generator: Any | None = None,
        levels: list[int] = None,
        knowledge_settings: Any | None = None,
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
        ks = knowledge_settings or {}
        self.l2_chunk_size = getattr(ks, "hierarchical_l2_chunk_size", self.DEFAULT_L2_CHUNK_SIZE)
        self.l2_chunk_overlap = getattr(
            ks, "hierarchical_l2_chunk_overlap", self.DEFAULT_L2_CHUNK_OVERLAP
        )
        self.l3_chunk_size = getattr(ks, "hierarchical_l3_chunk_size", self.DEFAULT_L3_CHUNK_SIZE)
        self.l3_chunk_overlap = getattr(
            ks, "hierarchical_l3_chunk_overlap", self.DEFAULT_L3_CHUNK_OVERLAP
        )

    async def index_document(
        self,
        document_id: str,
        dataset_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        chunking_config: Any | None = None,
        levels_override: list[int] | None = None,
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
            l2_segments: list[HierarchicalSegment] = []
            l3_segments: list[HierarchicalSegment] = []
            if IndexLevel.SECTION in levels or IndexLevel.PARAGRAPH in levels:
                l2_segments, l3_segments = await self._create_l2_l3_chunks(
                    document_id, dataset_id, text, metadata, chunking_config
                )

            l1_budget = int(IndexLevel.DOCUMENT in levels and self.summary_generator is not None)
            l2_budget = len(l2_segments) if IndexLevel.SECTION in levels else 0
            l3_budget = len(l3_segments) if IndexLevel.PARAGRAPH in levels else 0
            self._require_hierarchical_output_budget(l1_budget, l2_budget, l3_budget)

            if IndexLevel.SECTION in levels and self.summary_generator:
                await self._summarize_sections(l2_segments)

            # Provider, collection, embedding, and persistence calls are only
            # allowed after the complete hierarchy has passed its aggregate cap.
            vector_dim = await self._get_vector_dimension()
            base_collection = await self._resolve_base_collection(dataset_id, vector_dim)

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
                l1_segment = await self._create_l1_summary(document_id, dataset_id, text, metadata)
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

    @staticmethod
    def _require_hierarchical_output_budget(
        l1_count: int,
        l2_count: int,
        l3_count: int,
    ) -> None:
        if l1_count + l2_count + l3_count > MAX_CHUNK_OUTPUTS:
            raise ValidationFailedError(
                f"hierarchical output exceeds the {MAX_CHUNK_OUTPUTS} chunk limit"
            )

    async def _summarize_sections(self, segments: list[HierarchicalSegment]) -> None:
        if not self.summary_generator:
            return
        for segment in segments:
            if len(segment.text) <= 500:
                continue
            try:
                segment.summary = await self.summary_generator.summarize_section(segment.text)
            except Exception as exc:
                logger.debug(f"Failed to generate section summary: {exc}")

    async def _create_l2_l3_chunks(
        self,
        document_id: str,
        dataset_id: str,
        text: str,
        metadata: dict[str, Any],
        chunking_config: Any | None = None,
    ) -> tuple[list[HierarchicalSegment], list[HierarchicalSegment]]:
        """Create L2 section and L3 paragraph chunks with parent links."""
        from .chunking import ChunkingConfig, ChunkingMode, create_chunker

        # Base hierarchical config with default values (token-targeted)
        default_child_tokens = self.DEFAULT_L3_TOKEN_LIMIT
        default_parent_tokens = self.DEFAULT_L2_TOKEN_LIMIT
        config = ChunkingConfig(
            mode=ChunkingMode.HIERARCHICAL,
            parent_chunk_size=max(self.l2_chunk_size, default_parent_tokens * 4),
            parent_overlap=self.l2_chunk_overlap,
            child_chunk_size=max(self.l3_chunk_size, default_child_tokens * 4),
            child_overlap=self.l3_chunk_overlap,
            parent_mode="fixed",
            use_token_count=True,
            token_limit=default_child_tokens,
            parent_token_limit=default_parent_tokens,
            child_token_limit=default_child_tokens,
        )

        # Merge with user-provided chunking config if available
        if chunking_config:
            child_token_limit = (
                getattr(chunking_config, "child_token_limit", None)
                or getattr(chunking_config, "token_limit", None)
                or default_child_tokens
            )
            parent_token_limit = getattr(chunking_config, "parent_token_limit", None) or max(
                int(child_token_limit) * 4, 900
            )
            child_size = getattr(chunking_config, "child_chunk_size", None) or max(
                int(child_token_limit) * 4, default_child_tokens * 4
            )
            parent_size = getattr(chunking_config, "parent_chunk_size", None) or max(
                int(parent_token_limit) * 4, default_parent_tokens * 4
            )
            child_overlap = (
                getattr(chunking_config, "child_overlap", None)
                or getattr(chunking_config, "chunk_overlap", None)
                or self.l3_chunk_overlap
            )
            parent_overlap = getattr(chunking_config, "parent_overlap", None) or child_overlap

            config = ChunkingConfig(
                mode=ChunkingMode.HIERARCHICAL,
                parent_chunk_size=parent_size,
                parent_overlap=parent_overlap,
                child_chunk_size=child_size,
                child_overlap=child_overlap,
                parent_mode=str(getattr(chunking_config, "parent_mode", None) or "fixed"),
                use_token_count=bool(getattr(chunking_config, "use_token_count", True)),
                token_limit=int(getattr(chunking_config, "token_limit", None) or child_token_limit),
                min_chunk_tokens=getattr(chunking_config, "min_chunk_tokens", None),
                max_chunk_tokens=getattr(chunking_config, "max_chunk_tokens", None),
                parent_token_limit=parent_token_limit,
                child_token_limit=child_token_limit,
                separators=getattr(chunking_config, "separators", None)
                or ["\n\n\n", "\n\n", "\n", "。", ".", "！", "!", "？", "?", " "],
            )

            logger.info(
                f"[HierarchicalIndexer] Using user chunking config for {document_id}: "
                f"child_token_limit={child_token_limit}, parent_token_limit={parent_token_limit}, "
                f"child_size={child_size}, parent_size={parent_size}, "
                f"child_overlap={child_overlap}, parent_overlap={parent_overlap}"
            )

        chunker = create_chunker(config)
        parents = chunker.chunk(text)

        child_count = 0
        for parent in parents:
            child_count += len(parent.children)
            self._require_hierarchical_output_budget(0, len(parents), child_count)

        l2_segments: list[HierarchicalSegment] = []
        l3_segments: list[HierarchicalSegment] = []
        parent_map: dict[str, str] = {}
        l3_position = 0

        for idx, parent in enumerate(parents):
            segment_id = str(uuid.uuid4())
            parent_map[parent.hash_id] = segment_id

            l2_segments.append(
                HierarchicalSegment(
                    segment_id=segment_id,
                    document_id=document_id,
                    dataset_id=dataset_id,
                    level=IndexLevel.SECTION,
                    text=parent.text,
                    summary=None,
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
                            "parent_segment_id": segment_id,
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
        metadata: dict[str, Any],
    ) -> HierarchicalSegment | None:
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
        segments: list[HierarchicalSegment],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L3 segments to the main collection with transactional consistency."""
        if not segments:
            return

        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            dimension=vector_dim,
            collection_name=base_collection,
        )
        tenant_id = await self._dataset_tenant_id(dataset_id)

        # Generate embeddings
        texts = [s.text for s in segments]
        vectors = await self._embed_texts(texts)

        if not vectors or len(vectors) != len(segments):
            raise ValueError("Embedding count mismatch")

        # Build points and segment rows
        points = []
        segment_rows = []
        failed_segments = []

        for segment, vector in zip(segments, vectors, strict=True):
            if vector is None:
                failed_segments.append(segment.segment_id)
                continue

            payload = {
                "tenant_id": tenant_id,
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

            points.append(
                qmodels.PointStruct(
                    id=segment.segment_id,
                    vector=vector,
                    payload=payload,
                )
            )

            segment_rows.append(
                {
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
                }
            )

        if not points:
            logger.warning(f"No valid embeddings generated for {len(segments)} segments")
            return

        vector_success = False

        try:
            # Step 1: Upsert to Qdrant
            await self.vector_store.upsert(collection_name=collection, points=points)
            vector_success = True

            # Step 2: Save to database
            await self.db.insert_segments(segment_rows)

            if failed_segments:
                logger.warning(
                    f"Partial indexing: {len(failed_segments)} segments failed embedding"
                )

        except Exception as e:
            logger.error(f"Indexing failed - vector upsert success: {vector_success}: {e}")

            # Attempt rollback if vectors were written but DB failed
            if vector_success:
                logger.warning("Vector store succeeded but DB failed - attempting vector cleanup")
                try:
                    await self.vector_store.delete_points(
                        collection,
                        [str(point.id) for point in points],
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                    )
                    logger.info("Vector cleanup successful")
                except Exception as cleanup_error:
                    logger.error(f"Vector cleanup also failed: {cleanup_error}")
                    # Create chained exception to preserve both errors with full context
                    raise RuntimeError(
                        f"Indexing failed: {e}. Vector cleanup also failed: {cleanup_error}"
                    ) from e

            # Re-raise the original exception to preserve traceback
            raise

    async def _index_sections(
        self,
        segments: list[HierarchicalSegment],
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L2 sections to a separate collection with transactional consistency."""
        if not segments:
            return

        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            dimension=vector_dim,
            collection_name=f"{base_collection}{self.SECTION_COLLECTION_SUFFIX}",
        )
        tenant_id = await self._dataset_tenant_id(dataset_id)

        # Use summary for embedding if available, otherwise full text
        texts = [s.summary or s.text[:2000] for s in segments]
        vectors = await self._embed_texts(texts)

        if not vectors:
            return

        points = []
        segment_rows = []
        for segment, vector in zip(segments, vectors, strict=True):
            if vector is None:
                continue

            payload = {
                "tenant_id": tenant_id,
                "dataset_id": segment.dataset_id,
                "document_id": segment.document_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
                "level": segment.level,
                "text": segment.text[:500],  # Truncate for payload
                "summary": segment.summary,
                "content_type": "section",
            }

            points.append(
                qmodels.PointStruct(
                    id=segment.segment_id,
                    vector=vector,
                    payload=payload,
                )
            )
            segment_rows.append(
                {
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
                }
            )

        if not points:
            logger.warning(f"No valid embeddings for {len(segments)} sections")
            return

        # Transactional consistency: vectors first, then DB (with rollback on DB failure)
        vector_success = False
        try:
            await self.vector_store.upsert(collection_name=collection, points=points)
            vector_success = True

            await self.db.insert_segments(segment_rows)

        except Exception as e:
            logger.error(f"Section indexing failed - vector upsert success: {vector_success}: {e}")

            if vector_success:
                logger.warning("Section vectors written but DB failed - attempting vector cleanup")
                try:
                    await self.vector_store.delete_points(
                        collection,
                        [str(point.id) for point in points],
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                    )
                    logger.info("Section vector cleanup successful")
                except Exception as cleanup_error:
                    logger.error(f"Section vector cleanup also failed: {cleanup_error}")
                    raise RuntimeError(
                        f"Section indexing failed: {e}. Vector cleanup also failed: {cleanup_error}"
                    ) from e

            raise

    async def _index_summary(
        self,
        segment: HierarchicalSegment,
        dataset_id: str,
        vector_dim: int,
        base_collection: str,
    ) -> None:
        """Index L1 document summary with transactional consistency."""
        collection = await self._ensure_collection(
            dataset_id=dataset_id,
            dimension=vector_dim,
            collection_name=f"{base_collection}{self.SUMMARY_COLLECTION_SUFFIX}",
        )
        tenant_id = await self._dataset_tenant_id(dataset_id)

        # Embed the summary
        vectors = await self._embed_texts([segment.summary or segment.text])

        if not vectors or not vectors[0]:
            logger.warning(f"No embedding generated for document summary: {segment.document_id}")
            return

        payload = {
            "tenant_id": tenant_id,
            "dataset_id": segment.dataset_id,
            "document_id": segment.document_id,
            "segment_id": segment.segment_id,
            "level": segment.level,
            # Central lexical shadow writing consumes the canonical ``text``
            # payload for both v1 and bm25_v2. Summaries are independently
            # searchable documents, so never leave this field empty.
            "text": segment.summary or segment.text,
            "summary": segment.summary,
            "keywords": segment.keywords,
            "content_type": "document_summary",
        }

        point = qmodels.PointStruct(
            id=segment.segment_id,
            vector=vectors[0],
            payload=payload,
        )

        # Transactional consistency: vectors first, then DB (with rollback on DB failure)
        vector_success = False
        try:
            await self.vector_store.upsert(collection_name=collection, points=[point])
            vector_success = True

            await self.db.save_document_summary(
                {
                    "document_id": segment.document_id,
                    "summary": segment.summary,
                    "keywords": segment.keywords,
                    "topics": segment.metadata.get("topics", []),
                    "vector_id": segment.segment_id,
                }
            )

        except Exception as e:
            logger.error(
                f"Summary indexing failed for {segment.document_id} - vector upsert success: {vector_success}: {e}"
            )

            if vector_success:
                logger.warning(
                    f"Summary vector written but DB failed - attempting vector cleanup for {segment.document_id}"
                )
                try:
                    await self.vector_store.delete_points(
                        collection,
                        [str(point.id)],
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                    )
                    logger.info("Summary vector cleanup successful")
                except Exception as cleanup_error:
                    logger.error(f"Summary vector cleanup also failed: {cleanup_error}")
                    raise RuntimeError(
                        f"Summary indexing failed for {segment.document_id}: {e}. Vector cleanup also failed: {cleanup_error}"
                    ) from e

            raise

    async def _embed_texts(
        self,
        texts: list[str],
        max_retries: int = 3,
    ) -> list[list[float] | None]:
        """Generate embeddings for texts with retry.

        Args:
            texts: List of texts to embed
            max_retries: Maximum retry attempts

        Returns:
            List of embedding vectors (None for failed texts)
        """
        for attempt in range(max_retries):
            try:
                vectors = await self.embedder.embed_documents(texts)

                # Validate return result
                if len(vectors) != len(texts):
                    raise ValueError(f"Embedding count mismatch: {len(vectors)} vs {len(texts)}")

                # Check for None values in results
                none_count = sum(1 for v in vectors if v is None)
                if none_count > 0:
                    logger.warning(f"Embedding returned {none_count} None values")

                return vectors

            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Embedding failed after {max_retries} attempts: {e}")
                    return [None] * len(texts)

                wait_time = 0.5 * (attempt + 1)  # Linear backoff
                logger.warning(
                    f"Embedding attempt {attempt + 1} failed, retrying in {wait_time}s: {e}"
                )
                await asyncio.sleep(wait_time)

    async def _get_vector_dimension(self) -> int:
        """Get embedding dimension from embedder."""
        try:
            return self.embedder.dimension
        except AttributeError:
            return 1024  # Default for most models

    async def _ensure_collection(
        self,
        dataset_id: str,
        dimension: int,
        collection_name: str | None = None,
    ) -> str:
        """Ensure Qdrant collection exists."""
        lexical_config = LexicalConfig()
        tenant_id = ""
        base_collection = ""
        if self.db and hasattr(self.db, "get_dataset"):
            dataset = await self.db.get_dataset(dataset_id)
            if dataset:
                tenant_id = str(dataset.get("tenant_id") or "").strip()
                base_collection = str(dataset.get("collection_name") or "").strip()
                lexical_config = LexicalConfig.from_index_config(
                    dataset.get("index_config") or {}
                )
        if not tenant_id:
            raise ValueError("hierarchical indexing requires dataset tenant_id")
        if lexical_config.reads_bm25_v2:
            raise ValueError(
                "bm25_v2 active mode is read-only; roll back to lexical_v1 shadow "
                "before hierarchical indexing"
            )
        make_collection_name = getattr(self.vector_store, "make_collection_name", None)
        if callable(make_collection_name):
            target_collection = make_collection_name(
                dataset_id,
                dimension,
                collection_name,
            )
        else:
            target_collection = collection_name or f"kb_{dataset_id}_{dimension}"
        if not base_collection:
            base_collection = (
                make_collection_name(dataset_id, dimension, None)
                if callable(make_collection_name)
                else f"kb_{dataset_id}_{dimension}"
            )
        is_base_collection = target_collection == base_collection
        try:
            return await self.vector_store.ensure_collection(
                dataset_id=dataset_id,
                dimension=dimension,
                collection_name=collection_name,
                tenant_id=tenant_id,
                **(
                    {"lexical_config": lexical_config}
                    if lexical_config.configured and is_base_collection
                    else (
                        {
                            "lexical_config": LexicalConfig(),
                            "allow_lexical_transition": True,
                        }
                        if not is_base_collection
                        else {}
                    )
                ),
            )
        except Exception as e:
            if lexical_config.configured:
                raise
            logger.warning(f"Failed to ensure collection {collection_name}: {e}")
            return collection_name or f"kb_{dataset_id}_{dimension}"

    async def _dataset_tenant_id(self, dataset_id: str) -> str:
        """Resolve the immutable tenant scope used in every Qdrant payload."""
        if not self.db or not hasattr(self.db, "get_dataset"):
            raise ValueError("hierarchical indexing requires a dataset store")
        dataset = await self.db.get_dataset(dataset_id)
        tenant_id = str((dataset or {}).get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError("hierarchical indexing requires dataset tenant_id")
        return tenant_id

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

    async def delete_document_index(
        self,
        document_id: str,
        dataset_id: str,
        max_retries: int = 3,
    ) -> dict[str, bool]:
        """Delete all index entries for a document across all levels.

        Args:
            document_id: Document ID to delete
            dataset_id: Dataset ID
            max_retries: Maximum retry attempts per collection

        Returns:
            Dictionary mapping collection names to success status
        """
        dataset = await self.db.get_dataset(dataset_id)
        tenant_id = str((dataset or {}).get("tenant_id") or "").strip()
        if not tenant_id:
            raise ValueError("hierarchical deletion requires dataset tenant_id")

        base_collection = str((dataset or {}).get("collection_name") or "").strip()
        if not base_collection:
            vector_dim = await self._get_vector_dimension()
            make_collection_name = getattr(self.vector_store, "make_collection_name", None)
            base_collection = (
                make_collection_name(dataset_id, vector_dim, None)
                if callable(make_collection_name)
                else f"kb_{dataset_id}_{vector_dim}"
            )

        segments: list[dict[str, Any]] = []
        offset = 0
        page_size = 1000
        while True:
            page = await self.db.list_segments(
                dataset_id=dataset_id,
                document_id=document_id,
                limit=page_size,
                offset=offset,
            )
            if not page:
                break
            segments.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)

        document_summary = (
            await self.db.get_document_summary(document_id)
            if hasattr(self.db, "get_document_summary")
            else None
        )
        vector_ids = self.document_vector_ids(segments, document_summary)
        collections = self.document_collection_names(base_collection)
        results: dict[str, bool] = {}

        for level, collection in collections.items():
            point_ids = vector_ids[level]
            if not point_ids:
                results[collection] = True
                continue
            success = False
            last_error = None

            for attempt in range(max_retries):
                try:
                    await self.vector_store.delete_points(
                        collection,
                        point_ids,
                        tenant_id=tenant_id,
                        dataset_id=dataset_id,
                    )
                    success = True
                    break
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait_time = 0.1 * (attempt + 1)  # Exponential backoff
                        logger.warning(
                            f"Delete attempt {attempt + 1} failed for {collection}, "
                            f"retrying in {wait_time}s: {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"Failed to delete from {collection} after {max_retries} attempts: {e}"
                        )

            results[collection] = success
            if not success and last_error:
                logger.error(f"Final delete error for {collection}: {last_error}")

        return results
