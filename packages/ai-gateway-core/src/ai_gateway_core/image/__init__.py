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
    inflate_history_with_bytes,
    parse_image_size,
    resolve_image_routing,
    resolve_style,
)
from .image_state import (
    advance_latest_artifact_cas,
    compute_owner_scope,
    compute_request_hash,
    get_image_session,
    get_turn,
    get_turn_by_task,
    insert_turn,
    list_turns,
    lookup_idempotent,
    new_turn_id,
    record_idempotent,
    set_locked_style,
    update_turn_status,
    upsert_image_session,
)
from .thumbnail import make_thumbnail
from .watermark import apply_watermark_b64

__all__ = [
    "STYLE_MAP",
    "advance_latest_artifact_cas",
    "append_image_turns",
    "apply_watermark_b64",
    "build_gemini_contents_from_history",
    "compute_owner_scope",
    "compute_request_hash",
    "get_image_session",
    "get_turn",
    "get_turn_by_task",
    "inflate_history_with_bytes",
    "insert_turn",
    "list_turns",
    "lookup_idempotent",
    "make_thumbnail",
    "new_turn_id",
    "parse_image_size",
    "record_idempotent",
    "resolve_image_routing",
    "resolve_style",
    "send_image_callback",
    "set_locked_style",
    "update_turn_status",
    "upsert_image_session",
]
