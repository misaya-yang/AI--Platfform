"""Image watermark helper — re-export shim.

Phase 5d moved the canonical definitions to
``ai_gateway_core.image.watermark`` so gateway routes can import the
helper without a compile-time dependency on ``assistant_service``.
"""

from __future__ import annotations

from ai_gateway_core.image.watermark import apply_watermark_b64

__all__ = ["apply_watermark_b64"]
