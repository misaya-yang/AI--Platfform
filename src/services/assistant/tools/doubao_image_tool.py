"""Doubao SeedReam Image Generator (Volcengine/ByteDance).

Uses the OpenAI-compatible images.generate API on Volcengine ARK platform.
Model: doubao-seedream-5-0-260128
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_MODEL = "doubao-seedream-5-0-260128"


@dataclass
class DoubaoImageResult:
    success: bool
    images: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0


class DoubaoImageGenerator:
    """Image generator using Volcengine ARK (Doubao SeedReam)."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("ARK_API_KEY")
        self.model = model or os.getenv("DOUBAO_IMAGE_MODEL", _DEFAULT_MODEL)
        self.base_url = os.getenv("ARK_BASE_URL", _ARK_BASE_URL)
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
        size: str = "1024x1024",
    ) -> DoubaoImageResult:
        """Generate images via Volcengine ARK OpenAI-compatible API."""
        if not self.is_configured:
            return DoubaoImageResult(success=False, error="ARK_API_KEY not configured")

        start = time.time()

        # Normalize size format: "1024*1024" → "1024x1024"
        normalized_size = size.replace("*", "x") if size else "1024x1024"

        try:
            client = await self._get_client()

            body: dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "n": min(max(n, 1), 4),
                "size": normalized_size,
                "response_format": "b64_json",
            }

            response = await client.post(
                f"{self.base_url}/images/generations",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                json=body,
            )

            duration_ms = (time.time() - start) * 1000

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error("Doubao image API error: %s - %s", response.status_code, error_text)
                try:
                    error_msg = response.json().get("error", {}).get("message", error_text[:200])
                except Exception:
                    error_msg = error_text[:200]
                return DoubaoImageResult(success=False, error=f"API error: {response.status_code} - {error_msg}", duration_ms=duration_ms)

            result = response.json()
            images = []

            for i, item in enumerate(result.get("data", [])):
                b64_data = item.get("b64_json", "")
                if not b64_data:
                    # If URL response format
                    url = item.get("url")
                    if url:
                        try:
                            dl = await client.get(url)
                            if dl.status_code == 200:
                                b64_data = base64.b64encode(dl.content).decode("utf-8")
                        except Exception as e:
                            logger.warning("Failed to download Doubao image %d: %s", i, e)
                            continue

                if b64_data:
                    try:
                        size_bytes = len(base64.b64decode(b64_data))
                    except Exception:
                        size_bytes = len(b64_data) * 3 // 4
                    images.append({
                        "filename": f"doubao_image_{i + 1}.png",
                        "content_base64": b64_data,
                        "mime_type": "image/png",
                        "size_bytes": size_bytes,
                    })

            if not images:
                return DoubaoImageResult(success=False, error="No images returned", duration_ms=duration_ms)

            logger.info("Doubao generated %d image(s) in %.0fms", len(images), duration_ms)
            return DoubaoImageResult(success=True, images=images, duration_ms=duration_ms)

        except Exception as e:
            logger.error("Doubao image generation failed: %s", e)
            return DoubaoImageResult(
                success=False, error=str(e),
                duration_ms=(time.time() - start) * 1000,
            )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None


_doubao_generator: DoubaoImageGenerator | None = None


def get_doubao_image_generator() -> DoubaoImageGenerator:
    global _doubao_generator
    if _doubao_generator is None:
        _doubao_generator = DoubaoImageGenerator()
    return _doubao_generator
