"""Memory components for the Assistant runtime."""

from .chunker import ChunkConfig, MemoryChunk, chunk_markdown
from .indexer import (
    MemoryIndexDeleteResult,
    MemoryIndexer,
    MemoryIndexFinalizeResult,
    MemorySourceDeletionPendingError,
)
from .lifecycle import (
    MEMORY_LIFECYCLE_SCHEMA_VERSION,
    MemoryProviderLifecycle,
    MemoryThreatScan,
    MemoryWriteResult,
    bounded_memory_text,
    build_compaction_lineage,
    memory_hit_provenance,
    scan_memory_text,
    should_sync_turn_to_memory,
)
from .reflector import DailyMemoryReflector
from .retriever import HybridMemoryRetriever, MemorySearchHit
from .scope import public_source_label, scoped_collection_name
from .source_store import MemorySourceDocument, MemorySourceStore

__all__ = [
    "ChunkConfig",
    "MemoryChunk",
    "chunk_markdown",
    "MemoryIndexer",
    "MemoryIndexDeleteResult",
    "MemoryIndexFinalizeResult",
    "MemorySourceDeletionPendingError",
    "MEMORY_LIFECYCLE_SCHEMA_VERSION",
    "MemoryProviderLifecycle",
    "MemoryThreatScan",
    "MemoryWriteResult",
    "bounded_memory_text",
    "build_compaction_lineage",
    "memory_hit_provenance",
    "scan_memory_text",
    "should_sync_turn_to_memory",
    "DailyMemoryReflector",
    "HybridMemoryRetriever",
    "MemorySearchHit",
    "public_source_label",
    "scoped_collection_name",
    "MemorySourceDocument",
    "MemorySourceStore",
]
