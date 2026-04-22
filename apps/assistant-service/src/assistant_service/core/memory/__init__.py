"""
Three-layer memory system for enterprise AI assistant.

This module provides a hierarchical memory architecture:
- WorkingMemoryLayer: In-memory, fastest, for current session task state
- SessionMemoryLayer: Database-backed, persists across reconnections
- LongTermMemoryLayer: User-level persistent memory for preferences

The MemoryManager provides a unified interface across all layers.
"""

from .compressor import (
    ARTIFACT_PATTERN,
    MAX_PRESERVED_CODE_BLOCKS,
    MAX_PRESERVED_URLS,
    PRESERVE_PATTERNS,
    CompressedContext,
    ContextCompressor,
    LLMService,
)
from .memory_manager import (
    LongTermMemoryLayer,
    MemoryDatabase,
    MemoryLayer,
    MemoryManager,
    SessionMemoryLayer,
    WorkingMemoryLayer,
)

__all__ = [
    # Memory Manager
    "MemoryDatabase",
    "MemoryLayer",
    "WorkingMemoryLayer",
    "SessionMemoryLayer",
    "LongTermMemoryLayer",
    "MemoryManager",
    # Context Compressor
    "CompressedContext",
    "ContextCompressor",
    "LLMService",
    "PRESERVE_PATTERNS",
    "ARTIFACT_PATTERN",
    "MAX_PRESERVED_URLS",
    "MAX_PRESERVED_CODE_BLOCKS",
]
