"""ReAct phase enum.

The full ReAct execution loop was deleted in Phase A (the streaming-first
loop is the only execution path now). ``ReActPhase`` survives because
``core/assistant_service.py`` still uses these values as SSE event ``phase``
tags so the frontend can label tool-card sections (analyzing / thinking /
executing / observing / writing / completing). Renaming the tags is
out of scope here; we keep the enum as the single source of truth.
"""

from __future__ import annotations

from enum import Enum


class ReActPhase(str, Enum):
    """SSE event phase tag — labels which part of the agent turn produced the event."""

    ANALYZING = "analyzing"
    THINKING = "thinking"
    PLANNING = "planning"
    EXECUTING = "executing"
    OBSERVING = "observing"
    WRITING = "writing"
    COMPLETING = "completing"
