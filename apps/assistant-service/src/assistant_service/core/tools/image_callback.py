"""Image generation callback — re-export shim.

Phase 5d moved the canonical definition to
``ai_gateway_core.image.callback``.
"""

from __future__ import annotations

from ai_gateway_core.image.callback import send_image_callback

__all__ = ["send_image_callback"]
