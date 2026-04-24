"""
Image Style Presets — re-export shim.

Phase 5d moved the canonical definitions to
``ai_gateway_core.style_presets`` so gateway routes can import them
without pulling in ``assistant_service``. This file is kept as a
compatibility shim for AS-internal imports; delete once every
``from ...tools.style_presets`` site in AS migrates to the shared
module.
"""

from __future__ import annotations

from ai_gateway_core.enums import StylePreset
from ai_gateway_core.style_presets import (
    STYLE_PRESETS,
    StyleDefinition,
    compose_styled_prompt,
    needs_prompt_injection_for_dashscope,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)

__all__ = [
    "StylePreset",
    "StyleDefinition",
    "STYLE_PRESETS",
    "resolve_style_preset",
    "compose_styled_prompt",
    "resolve_dashscope_style_tag",
    "resolve_negative_prompt",
    "needs_prompt_injection_for_dashscope",
]
