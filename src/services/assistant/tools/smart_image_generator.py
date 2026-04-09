"""
Smart Image Generator.

Routes image generation across providers with safe fallback rules:
- Prefer Gemini native image generation when configured
- If Gemini fails with a non-safety error, fallback to DashScope Wanx when configured
- If Gemini is blocked by safety filters, DO NOT fallback (avoid provider bypass)
- Iterative editing (reference_image) is Gemini-only — DashScope does not support image input
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ....core.observability.logging import get_logger
from .doubao_image_tool import get_doubao_image_generator
from .gemini_image_tool import get_gemini_image_generator
from .image_generator_tool import get_image_generator

logger = get_logger(__name__)


@dataclass
class SmartImageGenerationResult:
    success: bool
    provider: str
    images: list[dict[str, Any]] = field(default_factory=list)
    text: str | None = None  # Model text response (e.g., edit description)
    error: str | None = None
    error_code: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    duration_ms: float = 0.0
    used_fallback: bool = False


class SmartImageGenerator:
    """
    Provider router for image generation.

    Routes to the preferred provider first, falls back to the other on non-safety errors.
    When reference_image is provided (iterative editing), always routes to Gemini.
    """

    async def generate(
        self,
        prompt: str,
        n: int = 1,
        size: str = "1024*1024",
        style: str = "<auto>",
        negative_prompt: str = "",
        aspect_ratio: str = "1:1",
        prefer_gemini: bool = True,
        prefer_doubao: bool = False,
        dashscope_model: str | None = None,
    ) -> SmartImageGenerationResult:
        """Single-turn image generation with provider routing.

        Multi-turn editing is handled at the API layer (via session history → Gemini
        generate_chat), so this method only handles single-turn prompt → image.
        """
        start = time.time()

        gemini = get_gemini_image_generator()
        dash = get_image_generator()
        doubao = get_doubao_image_generator()

        # Doubao/Volcengine first (when explicitly preferred)
        if prefer_doubao and doubao.is_configured:
            doubao_res = await doubao.generate(prompt=prompt, n=n, size=size)
            if doubao_res.success:
                return SmartImageGenerationResult(
                    success=True, provider="doubao",
                    images=doubao_res.images, duration_ms=doubao_res.duration_ms,
                )
            # Doubao failed: fallback to Gemini → DashScope
            logger.warning("Doubao image generation failed, trying fallback. err=%s", doubao_res.error)
            if gemini.is_configured:
                gemini_res = await gemini.generate(prompt=prompt, n=n, aspect_ratio=aspect_ratio)
                if gemini_res.success:
                    return SmartImageGenerationResult(
                        success=True, provider="google", images=gemini_res.images,
                        text=gemini_res.text, duration_ms=gemini_res.duration_ms, used_fallback=True,
                    )
            if dash.is_configured:
                dash_res = await dash.generate(prompt=prompt, negative_prompt=negative_prompt, size=size, style=style, n=n, model_override=dashscope_model)
                return SmartImageGenerationResult(
                    success=dash_res.success, provider="dashscope",
                    images=dash_res.images if dash_res.success else [],
                    error=dash_res.error, duration_ms=dash_res.duration_ms, used_fallback=True,
                )
            return SmartImageGenerationResult(
                success=False, provider="doubao", images=[],
                error=doubao_res.error, duration_ms=doubao_res.duration_ms,
            )

        # Gemini first (if preferred + configured)
        if prefer_gemini and gemini.is_configured:
            gemini_res = await gemini.generate(prompt=prompt, n=n, aspect_ratio=aspect_ratio)
            if gemini_res.success:
                return SmartImageGenerationResult(
                    success=True,
                    provider="google",
                    images=gemini_res.images,
                    text=gemini_res.text,
                    duration_ms=gemini_res.duration_ms,
                )

            # Safety-blocked: do not fallback across providers.
            if gemini_res.blocked or gemini_res.error_code == "GEMINI_IMAGE_BLOCKED":
                return SmartImageGenerationResult(
                    success=False,
                    provider="google",
                    images=[],
                    error=gemini_res.error,
                    error_code=gemini_res.error_code,
                    blocked=True,
                    block_reason=gemini_res.block_reason,
                    duration_ms=gemini_res.duration_ms,
                )

            # Non-safety Gemini error: fallback to DashScope when possible.
            if dash.is_configured:
                logger.warning(
                    "Gemini image generation failed, falling back to DashScope. err=%s",
                    gemini_res.error,
                )
                dash_res = await dash.generate(
                    prompt=prompt,
                    negative_prompt=negative_prompt,
                    size=size,
                    style=style,
                    n=n,
                    model_override=dashscope_model,
                )
                return SmartImageGenerationResult(
                    success=dash_res.success,
                    provider="dashscope",
                    images=dash_res.images if dash_res.success else [],
                    error=dash_res.error,
                    duration_ms=dash_res.duration_ms,
                    used_fallback=True,
                )

            return SmartImageGenerationResult(
                success=False,
                provider="google",
                images=[],
                error=gemini_res.error,
                error_code=gemini_res.error_code,
                duration_ms=gemini_res.duration_ms,
            )

        # DashScope first (when not prefer_gemini)
        if not prefer_gemini and dash.is_configured:
            dash_res = await dash.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                size=size,
                style=style,
                n=n,
            )
            if dash_res.success:
                return SmartImageGenerationResult(
                    success=True,
                    provider="dashscope",
                    images=dash_res.images,
                    duration_ms=dash_res.duration_ms,
                )

            # DashScope failed: fallback to Gemini when possible.
            if gemini.is_configured:
                logger.warning(
                    "DashScope image generation failed, falling back to Gemini. err=%s",
                    dash_res.error,
                )
                gemini_res = await gemini.generate(prompt=prompt, n=n, aspect_ratio=aspect_ratio)
                return SmartImageGenerationResult(
                    success=gemini_res.success,
                    provider="google",
                    images=gemini_res.images if gemini_res.success else [],
                    text=gemini_res.text,
                    error=gemini_res.error,
                    error_code=gemini_res.error_code,
                    blocked=gemini_res.blocked,
                    block_reason=gemini_res.block_reason,
                    duration_ms=gemini_res.duration_ms,
                    used_fallback=True,
                )

            return SmartImageGenerationResult(
                success=False,
                provider="dashscope",
                images=[],
                error=dash_res.error,
                duration_ms=dash_res.duration_ms,
            )

        # Single provider fallback (only one configured)
        if dash.is_configured:
            dash_res = await dash.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                size=size,
                style=style,
                n=n,
            )
            return SmartImageGenerationResult(
                success=dash_res.success,
                provider="dashscope",
                images=dash_res.images if dash_res.success else [],
                error=dash_res.error,
                duration_ms=dash_res.duration_ms,
            )

        if gemini.is_configured:
            gemini_res = await gemini.generate(prompt=prompt, n=n, aspect_ratio=aspect_ratio)
            return SmartImageGenerationResult(
                success=gemini_res.success,
                provider="google",
                images=gemini_res.images if gemini_res.success else [],
                text=gemini_res.text,
                error=gemini_res.error,
                error_code=gemini_res.error_code,
                blocked=gemini_res.blocked,
                block_reason=gemini_res.block_reason,
                duration_ms=gemini_res.duration_ms,
            )

        # No providers configured
        duration_ms = (time.time() - start) * 1000
        return SmartImageGenerationResult(
            success=False,
            provider="none",
            images=[],
            error="No image generation provider configured (missing GEMINI_API_KEY/GOOGLE_API_KEY and DASHSCOPE_API_KEY)",
            duration_ms=duration_ms,
        )


_smart_image_generator: SmartImageGenerator | None = None


def get_smart_image_generator(prefer_gemini: bool = True) -> SmartImageGenerator:
    global _smart_image_generator
    if _smart_image_generator is None:
        _smart_image_generator = SmartImageGenerator()
    return _smart_image_generator
