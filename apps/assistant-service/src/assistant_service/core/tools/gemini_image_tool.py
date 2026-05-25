"""
Gemini Native Image Generation & Multi-Turn Editing Tool

Uses gemini-3.1-flash-image-preview with multi-turn conversation history:
- Text → Image: pure prompt generation
- Multi-turn chat: full conversation history (text + images) for iterative editing
- The MODEL decides whether to edit a previous image or create something new

Architecture: caller builds `contents` array from session history,
this tool just sends it to Gemini generateContent API.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ai_gateway_core.config import resolve_google
from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GeminiImageResult:
    """Result of Gemini image generation."""

    success: bool
    images: list[dict[str, Any]] = field(default_factory=list)
    text: str | None = None
    error: str | None = None
    error_code: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    duration_ms: float = 0
    provider: str = "google"


class GeminiImageGenerator:
    """Image generator using Gemini multi-turn conversation API.

    Supports two calling modes:
    1. Simple: generate(prompt=...) — single turn text-to-image
    2. Multi-turn: generate_chat(contents=...) — full conversation with history

    Endpoint selection honours the per-domain ``resolve_google("image")``
    helper, so operators can route image gen to Vertex Express (free
    trial) while keeping chat on AI Studio, or vice versa. See
    ``ai_gateway_core.config.endpoints`` for the env-var contract.
    """

    DEFAULT_MODEL = "gemini-3.1-flash-image-preview"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        # Resolve via helper when no explicit key is passed — picks up
        # GOOGLE_IMAGE_BACKEND / VERTEX_IMAGE_API_KEY / shared fallbacks.
        if api_key:
            self.api_key = api_key
            self.base_url = "https://generativelanguage.googleapis.com"
            self.backend = "ai_studio"
        else:
            resolved_key, resolved_base_url, resolved_backend = resolve_google("image")
            self.api_key = resolved_key
            self.base_url = resolved_base_url
            self.backend = resolved_backend
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
        """Simple single-turn generation (backward compatible).

        For multi-turn editing, use generate_chat() instead.
        """
        parts: list[dict[str, Any]] = []

        if reference_image:
            img_data, mime = self._parse_data_url(reference_image, reference_mime_type)
            parts.append({"inlineData": {"mimeType": mime, "data": img_data}})

        parts.append({"text": prompt})
        contents = [{"role": "user", "parts": parts}]

        return await self._call_api(contents, n, aspect_ratio, image_size)

    async def generate_chat(
        self,
        contents: list[dict[str, Any]],
        n: int = 1,
        aspect_ratio: str = "1:1",
        image_size: str | None = None,
    ) -> GeminiImageResult:
        """Multi-turn image generation with full conversation history.

        Args:
            contents: Gemini contents array — list of {role, parts} dicts.
                      Previous model responses with images should be included.
            n: Number of images (1-4)
            aspect_ratio: For new generation only
            image_size: Output resolution
        """
        return await self._call_api(contents, n, aspect_ratio, image_size)

    async def _call_api(
        self,
        contents: list[dict[str, Any]],
        n: int = 1,
        aspect_ratio: str = "1:1",
        image_size: str | None = None,
    ) -> GeminiImageResult:
        """Core API call to Gemini generateContent."""
        if not self.is_configured:
            return GeminiImageResult(success=False, error="Google API key not configured")

        start_time = time.time()

        try:
            client = await self._get_client()
            # Vertex Express Mode uses a different path shape than AI Studio —
            # /v1/publishers/google/models/{model}:generateContent with the
            # key as ?key=AQ.xxx query param (same as chat path in
            # model_registry._google_endpoint). Falling back to AI Studio
            # when backend isn't set keeps the public-facing behaviour
            # unchanged.
            if self.backend == "vertex":
                endpoint = (
                    f"{self.base_url}/v1/publishers/google/models/"
                    f"{self.model}:generateContent?key={self.api_key}"
                )
            else:
                endpoint = f"{self.base_url}/v1beta/models/{self.model}:generateContent"

            gen_config: dict[str, Any] = {
                "responseModalities": ["TEXT", "IMAGE"],
                "candidateCount": max(1, min(int(n or 1), 4)),
            }
            image_config: dict[str, Any] = {}
            if aspect_ratio:
                image_config["aspectRatio"] = aspect_ratio
            if image_size:
                image_config["outputImageSize"] = image_size
            if image_config:
                gen_config["imageConfig"] = image_config

            body = {
                "contents": contents,
                "generationConfig": gen_config,
            }

            turns = len(contents)
            logger.info(
                "Gemini image API: %d turns, model=%s", turns, self.model,
            )

            # Vertex Express embeds the key in the URL (?key=) and must
            # not also set x-goog-api-key — the server rejects duplicate
            # auth. AI Studio accepts both but the header is the canonical
            # form, so keep that path unchanged.
            headers = {"Content-Type": "application/json"}
            if self.backend != "vertex":
                headers["x-goog-api-key"] = self.api_key

            response = await client.post(
                endpoint,
                headers=headers,
                json=body,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error("Gemini image API error: %s - %s", response.status_code, error_text[:500])
                try:
                    error_msg = response.json().get("error", {}).get("message", error_text[:200])
                except Exception:
                    error_msg = error_text[:200]
                return GeminiImageResult(
                    success=False, error=f"API error: {response.status_code} - {error_msg}",
                    duration_ms=(time.time() - start_time) * 1000,
                )

            result = response.json()

            # Safety blocking
            pf = result.get("promptFeedback") or result.get("prompt_feedback") or {}
            block = pf.get("blockReason") or pf.get("block_reason")
            if block:
                return GeminiImageResult(
                    success=False, error="Image generation blocked by safety filters",
                    error_code="GEMINI_IMAGE_BLOCKED", blocked=True, block_reason=str(block),
                    duration_ms=(time.time() - start_time) * 1000,
                )

            images, text_response = self._extract_response(result)
            duration_ms = (time.time() - start_time) * 1000

            if not images:
                logger.warning("Gemini returned no images. Text: %s", (text_response or "")[:200])
                return GeminiImageResult(
                    success=False, text=text_response,
                    error=text_response or "Model did not generate images.",
                    error_code="GEMINI_NO_IMAGE", duration_ms=duration_ms,
                )

            logger.info("Gemini generated %d image(s) in %.0fms", len(images), duration_ms)
            return GeminiImageResult(success=True, images=images, text=text_response, duration_ms=duration_ms)

        except Exception as e:
            logger.error("Gemini image generation failed: %s", e)
            return GeminiImageResult(
                success=False, error=str(e), error_code="GEMINI_IMAGE_ERROR",
                duration_ms=(time.time() - start_time) * 1000,
            )

    def _extract_response(self, response: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Extract images and optional text from Gemini response."""
        images, text_parts = [], []
        for candidate in response.get("candidates", []):
            for i, part in enumerate(candidate.get("content", {}).get("parts", [])):
                if "text" in part:
                    text_parts.append(part["text"])
                inline_data = part.get("inlineData") or part.get("inline_data")
                if inline_data:
                    mime = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                    data = inline_data.get("data", "")
                    if data:
                        try:
                            size_bytes = len(base64.b64decode(data))
                        except Exception:
                            size_bytes = len(data) * 3 // 4
                        img_entry: dict[str, Any] = {
                            "filename": f"gemini_image_{i + 1}.png",
                            "content_base64": data,
                            "mime_type": mime,
                            "size_bytes": size_bytes,
                        }
                        # Preserve thought_signature for multi-turn (required by Gemini 3.x)
                        thought_sig = part.get("thoughtSignature") or part.get("thought_signature")
                        if thought_sig:
                            img_entry["thought_signature"] = thought_sig
                        images.append(img_entry)
        return images, "\n".join(text_parts) if text_parts else None

    @staticmethod
    def _parse_data_url(data_url: str, default_mime: str = "image/png") -> tuple[str, str]:
        """Parse a data URL or raw base64 into (base64_data, mime_type)."""
        if data_url.startswith("data:"):
            comma = data_url.index(",")
            header = data_url[:comma]
            mime = header.split(";")[0].split(":")[1] if "image/" in header else default_mime
            return data_url[comma + 1:], mime
        return data_url, default_mime

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


_gemini_generator: GeminiImageGenerator | None = None


def get_gemini_image_generator() -> GeminiImageGenerator:
    global _gemini_generator
    if _gemini_generator is None:
        _gemini_generator = GeminiImageGenerator()
    return _gemini_generator
