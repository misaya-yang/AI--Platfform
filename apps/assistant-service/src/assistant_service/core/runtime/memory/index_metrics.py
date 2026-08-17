"""Process-local counters for memory index short-circuit tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MemoryIndexMetrics:
    """Exercisable counters on the shipped index path."""

    chunk_markdown_calls: int = 0
    embed_calls: int = 0
    short_circuits: int = 0

    def reset(self) -> None:
        self.chunk_markdown_calls = 0
        self.embed_calls = 0
        self.short_circuits = 0


memory_index_metrics = MemoryIndexMetrics()
