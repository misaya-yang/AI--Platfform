"""Process-local counters for assistant trace persistence.

Follows the same pattern as ``runtime.memory.index_metrics``: exercisable
counters on the shipped trace-writer path, used by tests to prove SQL
statement ceilings (SPO-03 gate: 25 deltas ≤ 4 statements).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TraceWriterMetrics:
    """Exercisable counters on the shipped trace writer path."""

    sql_statements: int = 0

    def reset(self) -> None:
        self.sql_statements = 0


trace_writer_metrics = TraceWriterMetrics()
