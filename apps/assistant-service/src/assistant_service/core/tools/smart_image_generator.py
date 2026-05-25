"""
Smart Image Generator.

Routes image generation across providers with safe fallback rules:
- Prefer Gemini native image generation when configured
- If Gemini fails with a non-safety error, fallback to DashScope Wanx when configured
- If Gemini is blocked by safety filters, DO NOT fallback (avoid provider bypass)
- Iterative editing (reference_image) is Gemini-only — DashScope does not support image input

Style handling contract (since 2026-04-16):
- ``prompt`` is the caller's final prompt. For public /generate-image requests
  the API layer pre-injects the StylePreset modifier via
  ``compose_styled_prompt``, so Gemini and Doubao see the style hint directly.
- ``style`` and ``negative_prompt`` are DashScope-specific tags/filters. They
  are only forwarded to DashScope. For presets without a native DashScope tag
  we pass ``<auto>``; the prompt-level modifier still drives the rendering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger
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
        size: str = "1536*1536",
        style: str = "<auto>",
        negative_prompt: str = "",
        aspect_ratio: str = "1:1",
        prefer_gemini: bool = True,
        prefer_doubao: bool = False,
        dashscope_model: str | None = None,
        image_model_override: dict[str, Any] | None = None,
    ) -> SmartImageGenerationResult:
        """Single-turn image generation with provider routing.

        Multi-turn editing is handled at the API layer (via session history → Gemini
        generate_chat), so this method only handles single-turn prompt → image.
        """
        start = time.time()

        if image_model_override and image_model_override.get("enabled"):
            return await self._generate_with_override(
                image_model_override,
                prompt=prompt,
                n=n,
                size=size,
                style=style,
                negative_prompt=negative_prompt,
                aspect_ratio=aspect_ratio,
                start_time=start,
            )

        gemini = get_gemini_image_generator()
        dash = get_image_generator()
        doubao = get_doubao_image_generator()

        # Doubao/Volcengine first (when explicitly preferred)
        # Map internal "1536*1536" format to Gemini "1536x1536" format.
        gemini_image_size = size.replace("*", "x") if size else None

        if prefer_doubao and doubao.is_configured:
            doubao_res = await doubao.generate(prompt=prompt, n=n, size=size)
            if doubao_res.success:
                return SmartImageGenerationResult(
                    success=True,
                    provider="doubao",
                    images=doubao_res.images,
                    duration_ms=doubao_res.duration_ms,
                )
            # Doubao failed: fallback to Gemini → DashScope
            logger.warning(
                "Doubao image generation failed, trying fallback. err=%s", doubao_res.error
            )
            if gemini.is_configured:
                gemini_res = await gemini.generate(
                    prompt=prompt, n=n, aspect_ratio=aspect_ratio, image_size=gemini_image_size
                )
                if gemini_res.success:
                    return SmartImageGenerationResult(
                        success=True,
                        provider="google",
                        images=gemini_res.images,
                        text=gemini_res.text,
                        duration_ms=gemini_res.duration_ms,
                        used_fallback=True,
                    )
            if dash.is_configured:
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
                provider="doubao",
                images=[],
                error=doubao_res.error,
                duration_ms=doubao_res.duration_ms,
            )

        # Gemini first (if preferred + configured)
        if prefer_gemini and gemini.is_configured:
            gemini_res = await gemini.generate(
                prompt=prompt, n=n, aspect_ratio=aspect_ratio, image_size=gemini_image_size
            )
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
                model_override=dashscope_model,
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
                model_override=dashscope_model,
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

    async def _generate_with_override(
        self,
        override: dict[str, Any],
        *,
        prompt: str,
        n: int,
        size: str,
        style: str,
        negative_prompt: str,
        aspect_ratio: str,
        start_time: float,
    ) -> SmartImageGenerationResult:
        """Generate with an explicit Gateway-injected provider/model config.

        Explicit image overrides do not fall back to another provider: the
        operator selected a concrete image API in Gateway control plane.
        """
        provider_id = str(override.get("provider_id") or "").lower()
        runtime_provider = str(override.get("provider") or "").lower()
        model_id = str(override.get("model_id") or override.get("model") or "")
        api_key = override.get("_api_key")
        base_url = override.get("base_url")

        if not api_key:
            return SmartImageGenerationResult(
                success=False,
                provider=provider_id or "unknown",
                images=[],
                error="Gateway image provider API key is missing",
                error_code="provider_unavailable",
                duration_ms=(time.time() - start_time) * 1000,
            )

        gemini_image_size = size.replace("*", "x") if size else None
        if provider_id in {"google", "google-vertex"} or runtime_provider in {"gemini", "vertex"}:
            from .gemini_image_tool import GeminiImageGenerator

            backend = (
                "vertex"
                if runtime_provider == "vertex" or provider_id == "google-vertex"
                else "ai_studio"
            )
            gemini = GeminiImageGenerator(
                api_key=str(api_key),
                model=model_id or None,
                base_url=str(base_url) if base_url else None,
                backend=backend,
            )
            gemini_res = await gemini.generate(
                prompt=prompt,
                n=n,
                aspect_ratio=aspect_ratio,
                image_size=gemini_image_size,
            )
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

        if provider_id == "dashscope" or runtime_provider == "dashscope":
            from .image_generator_tool import DashScopeImageGenerator

            dash = DashScopeImageGenerator(
                api_key=str(api_key),
                model=model_id or None,
                base_url=str(base_url) if base_url else None,
            )
            dash_res = await dash.generate(
                prompt=prompt,
                negative_prompt=negative_prompt,
                size=size,
                style=style,
                n=n,
                model_override=model_id or None,
            )
            return SmartImageGenerationResult(
                success=dash_res.success,
                provider="dashscope",
                images=dash_res.images if dash_res.success else [],
                error=dash_res.error,
                duration_ms=dash_res.duration_ms,
            )

        return SmartImageGenerationResult(
            success=False,
            provider=provider_id or runtime_provider or "unknown",
            images=[],
            error=f"Unsupported image provider override: {provider_id or runtime_provider}",
            error_code="provider_unsupported",
            duration_ms=(time.time() - start_time) * 1000,
        )


_smart_image_generator: SmartImageGenerator | None = None


def get_smart_image_generator() -> SmartImageGenerator:
    global _smart_image_generator
    if _smart_image_generator is None:
        _smart_image_generator = SmartImageGenerator()
    return _smart_image_generator
