"""
Gemini Native Image Generation & Editing Tool for Assistant Service

Uses gemini-3.1-flash-image-preview for:
- Text-to-image generation
- Iterative image editing (send previous image + instruction)
- Multi-resolution output (0.5K, 1K, 2K, 4K)
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ....core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GeminiImageResult:
    """Result of Gemini image generation."""

    success: bool
    images: list[dict[str, Any]] = field(default_factory=list)
    text: str | None = None  # Model may return text alongside images
    error: str | None = None
    error_code: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    duration_ms: float = 0


class GeminiImageGenerator:
    """Image generator using Google Gemini Native Image API (Nano Banana 2).

    Supports:
    - Text → Image: pure text prompt generation
    - Image + Text → Image: iterative editing (send reference image + edit instruction)
    """

    BASE_URL = "https://generativelanguage.googleapis.com"
    # Nano Banana 2: gemini-3.1-flash-image-preview — fast, multi-resolution
    DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
    # Fallback for compatibility
    FALLBACK_MODEL = "gemini-2.5-flash-image"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model = model or self.DEFAULT_MODEL
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def generate(
        self,
        prompt: str,
        n: int = 1,
        aspect_ratio: str = "1:1",
        reference_image: str | None = None,
        reference_mime_type: str = "image/png",
        image_size: str | None = None,
    ) -> GeminiImageResult:
        """
        Generate or edit images using Gemini.

        Args:
            prompt: Text description or edit instruction
            n: Number of images (1-4)
            aspect_ratio: "1:1", "16:9", "9:16", "4:3", "3:4"
            reference_image: Base64-encoded image for iterative editing (optional)
            reference_mime_type: MIME type of reference image
            image_size: Output resolution — "512px", "1024px", "2048px", "4096px"
        """
        if not self.is_configured:
            return GeminiImageResult(success=False, error="Google API key not configured")

        start_time = time.time()

        try:
            client = await self._get_client()
            endpoint = f"{self.BASE_URL}/v1beta/models/{self.model}:generateContent"

            # Build content parts
            parts: list[dict[str, Any]] = []

            # If reference image provided → iterative editing mode
            if reference_image:
                # Strip data URL prefix if present
                img_data = reference_image
                if img_data.startswith("data:"):
                    # Extract base64 from "data:image/png;base64,..."
                    comma_idx = img_data.index(",")
                    header = img_data[:comma_idx]
                    img_data = img_data[comma_idx + 1:]
                    if "image/" in header:
                        reference_mime_type = header.split(";")[0].split(":")[1]

                parts.append({
                    "inlineData": {
                        "mimeType": reference_mime_type,
                        "data": img_data,
                    }
                })
                logger.info("Iterative edit mode: reference image + instruction")

            parts.append({"text": prompt})

            # Generation config
            gen_config: dict[str, Any] = {
                "responseModalities": ["TEXT", "IMAGE"],
                "candidateCount": max(1, min(int(n or 1), 4)),
            }

            # Image config
            image_config: dict[str, Any] = {}
            if aspect_ratio and not reference_image:
                # Only set aspect ratio for new generation, not editing
                image_config["aspectRatio"] = aspect_ratio
            if image_size:
                image_config["outputImageSize"] = image_size

            if image_config:
                gen_config["imageConfig"] = image_config

            body = {
                "contents": [{"parts": parts}],
                "generationConfig": gen_config,
            }

            logger.info(
                "Gemini image %s: %s (model=%s)",
                "edit" if reference_image else "generate",
                prompt[:60],
                self.model,
            )

            response = await client.post(
                endpoint,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
                json=body,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error("Gemini image API error: %s - %s", response.status_code, error_text[:500])
                # Try fallback model if primary fails with 404
                if response.status_code == 404 and self.model != self.FALLBACK_MODEL:
                    logger.info("Retrying with fallback model: %s", self.FALLBACK_MODEL)
                    self.model = self.FALLBACK_MODEL
                    return await self.generate(
                        prompt, n, aspect_ratio, reference_image, reference_mime_type, image_size,
                    )
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", error_text[:200])
                except Exception:
                    error_msg = error_text[:200]
                return GeminiImageResult(
                    success=False,
                    error=f"API error: {response.status_code} - {error_msg}",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            result = response.json()

            # Safety blocking
            prompt_feedback = result.get("promptFeedback") or result.get("prompt_feedback") or {}
            block_reason = prompt_feedback.get("blockReason") or prompt_feedback.get("block_reason")
            if block_reason:
                duration_ms = (time.time() - start_time) * 1000
                logger.warning("Gemini image blocked: %s", block_reason)
                return GeminiImageResult(
                    success=False,
                    error="Image generation blocked by safety filters",
                    error_code="GEMINI_IMAGE_BLOCKED",
                    blocked=True,
                    block_reason=str(block_reason),
                    duration_ms=duration_ms,
                )

            images, text_response = self._extract_response(result)
            duration_ms = (time.time() - start_time) * 1000

            if not images:
                logger.warning("Gemini returned no images. Text: %s", (text_response or "")[:200])
                return GeminiImageResult(
                    success=False,
                    text=text_response,
                    error=text_response or "Model did not generate images. Try a different prompt.",
                    error_code="GEMINI_NO_IMAGE",
                    duration_ms=duration_ms,
                )

            logger.info("Gemini generated %d image(s) in %.0fms", len(images), duration_ms)

            return GeminiImageResult(
                success=True,
                images=images,
                text=text_response,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.error("Gemini image generation failed: %s", e)
            return GeminiImageResult(
                success=False,
                error=str(e),
                error_code="GEMINI_IMAGE_ERROR",
                duration_ms=(time.time() - start_time) * 1000,
            )

    def _extract_response(self, response: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Extract images and optional text from Gemini response."""
        images = []
        text_parts = []

        for candidate in response.get("candidates", []):
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            for i, part in enumerate(parts):
                # Text part
                if "text" in part:
                    text_parts.append(part["text"])

                # Image part
                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data:
                    mime_type = (
                        inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                    )
                    data = inline_data.get("data", "")
                    if data:
                        try:
                            size_bytes = len(base64.b64decode(data))
                        except Exception:
                            size_bytes = len(data) * 3 // 4
                        images.append({
                            "filename": f"gemini_image_{i + 1}.png",
                            "content_base64": data,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                        })

        text_response = "\n".join(text_parts) if text_parts else None
        return images, text_response

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


# Global instance
_gemini_generator: GeminiImageGenerator | None = None


def get_gemini_image_generator() -> GeminiImageGenerator:
    global _gemini_generator
    if _gemini_generator is None:
        _gemini_generator = GeminiImageGenerator()
    return _gemini_generator
