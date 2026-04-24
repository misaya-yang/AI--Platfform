"""Shared helpers for image generation endpoints — re-export shim.

Phase 5d moved the canonical definitions to ``ai_gateway_core.image``
so gateway routes no longer import from ``assistant_service``. This
shim keeps existing ``from .image_helpers import …`` sites inside AS
working; delete once every AS call site migrates to the shared module.
"""

from __future__ import annotations

from ai_gateway_core.image import (
    STYLE_MAP,
    append_image_turns,
    build_gemini_contents_from_history,
    parse_image_size,
    resolve_image_routing,
    resolve_style,
)

__all__ = [
    "STYLE_MAP",
    "append_image_turns",
    "build_gemini_contents_from_history",
    "parse_image_size",
    "resolve_image_routing",
    "resolve_style",
]
