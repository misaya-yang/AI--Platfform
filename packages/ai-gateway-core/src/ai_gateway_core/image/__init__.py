"""Shared image-generation helpers.

Moved from ``assistant_service.core.tools.*`` in Phase 5d so gateway
routes can import these pure utilities (size parsing, routing, history
builders, watermarking, webhook callback) without a compile-time
dependency on ``assistant_service``.
"""

from .callback import send_image_callback
from .helpers import (
    STYLE_MAP,
    append_image_turns,
    build_gemini_contents_from_history,
    parse_image_size,
    resolve_image_routing,
    resolve_style,
)
from .watermark import apply_watermark_b64

__all__ = [
    "STYLE_MAP",
    "append_image_turns",
    "apply_watermark_b64",
    "build_gemini_contents_from_history",
    "parse_image_size",
    "resolve_image_routing",
    "resolve_style",
    "send_image_callback",
]
