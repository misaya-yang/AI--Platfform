"""Task types — re-export shim.

Phase 5d moved ``process_file_task`` to
``ai_gateway_core.tasks.task_types`` (with FileProcessor annotation
TYPE_CHECKING-only so the shared package doesn't drag AS's document
pipeline in).
"""

from __future__ import annotations

from ai_gateway_core.tasks.task_types import process_file_task

__all__ = ["process_file_task"]
