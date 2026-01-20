"""
Gemini Native Image Generation Tool for Assistant Service

Provides text-to-image generation using Google Gemini's native image generation
capability (aka "nano banana").

Uses gemini-2.5-flash-image model for high-quality image generation.
"""

from __future__ import annotations

import base64
import os
import time
import httpx
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from ....core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GeminiImageResult:
    """Result of Gemini image generation."""
    success: bool
    images: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    duration_ms: float = 0


class GeminiImageGenerator:
    """Image generator using Google Gemini Native Image API (Nano Banana)."""

    BASE_URL = "https://generativelanguage.googleapis.com"
    # Nano Banana: gemini-2.5-flash-image - 快速、便宜
    # Nano Banana Pro: gemini-3-pro-image-preview - 高质量、贵
    DEFAULT_MODEL = "gemini-2.5-flash-image"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self._client: Optional[httpx.AsyncClient] = None

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
    ) -> GeminiImageResult:
        """
        Generate images from text prompt using Gemini native image generation.

        Args:
            prompt: Text description of the image to generate
            n: Number of images to generate (1-4)
            aspect_ratio: Image aspect ratio ("1:1", "16:9", "9:16", "4:3", "3:4")
        """
        if not self.is_configured:
            return GeminiImageResult(
                success=False,
                error="Google API key not configured"
            )

        start_time = time.time()

        try:
            client = await self._get_client()

            # Build request for Gemini image generation
            # Using generateContent with responseModalities to enable image output
            # Endpoint format: https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
            endpoint = f"{self.BASE_URL}/v1beta/models/{self.DEFAULT_MODEL}:generateContent"

            # Request body for native image generation
            # Based on official docs: https://ai.google.dev/gemini-api/docs/image-generation
            body = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt}
                        ]
                    }
                ],
                "generationConfig": {
                    # IMAGE modality is required for image generation
                    # TEXT can optionally be included if you want text explanations too
                    "responseModalities": ["TEXT", "IMAGE"],
                },
            }

            logger.info(f"Submitting Gemini image generation: {prompt[:50]}...")

            # API key must be passed as x-goog-api-key header (NOT query param)
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
                logger.error(f"Gemini image generation failed: {response.status_code} - {error_text[:500]}")
                # Parse error for user-friendly message
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

            # Debug: Log response structure to diagnose issues
            logger.debug(f"Gemini response keys: {result.keys()}")
            if "candidates" in result:
                for i, cand in enumerate(result["candidates"]):
                    content = cand.get("content", {})
                    parts = content.get("parts", [])
                    logger.debug(f"Candidate {i}: {len(parts)} parts")
                    for j, part in enumerate(parts):
                        part_keys = list(part.keys())
                        logger.debug(f"  Part {j} keys: {part_keys}")
                        if "text" in part:
                            logger.debug(f"  Part {j} text: {part['text'][:100]}...")

            images = self._extract_images(result)

            duration_ms = (time.time() - start_time) * 1000

            if not images:
                # Log detailed info about the response
                text_parts = []
                for cand in result.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part:
                            text_parts.append(part["text"])
                if text_parts:
                    logger.warning(f"Gemini returned text instead of images: {text_parts[0][:200]}...")
                else:
                    logger.warning("Gemini did not return images or text")
                return GeminiImageResult(
                    success=False,
                    error="Model did not generate images. Try using a different provider.",
                    duration_ms=duration_ms,
                )

            logger.info(f"Gemini generated {len(images)} image(s) in {duration_ms:.0f}ms")

            return GeminiImageResult(
                success=True,
                images=images,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.error(f"Gemini image generation failed: {e}")
            return GeminiImageResult(
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    def _extract_images(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract images from Gemini response."""
        images = []

        candidates = response.get("candidates", [])
        for candidate in candidates:
            content = candidate.get("content", {})
            parts = content.get("parts", [])

            for i, part in enumerate(parts):
                # Check for inline image data
                if "inlineData" in part:
                    inline_data = part["inlineData"]
                    mime_type = inline_data.get("mimeType", "image/png")
                    data = inline_data.get("data", "")

                    if data:
                        # Calculate size from base64
                        try:
                            decoded = base64.b64decode(data)
                            size_bytes = len(decoded)
                        except Exception:
                            size_bytes = len(data) * 3 // 4  # Approximate

                        images.append({
                            "filename": f"gemini_image_{i+1}.png",
                            "content_base64": data,
                            "mime_type": mime_type,
                            "size_bytes": size_bytes,
                        })

        return images

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Global instance
_gemini_generator: Optional[GeminiImageGenerator] = None


def get_gemini_image_generator() -> GeminiImageGenerator:
    """Get or create the global Gemini image generator instance."""
    global _gemini_generator
    if _gemini_generator is None:
        _gemini_generator = GeminiImageGenerator()
    return _gemini_generator
