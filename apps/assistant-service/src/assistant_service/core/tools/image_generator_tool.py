"""
Image Generation Tool for Assistant Service

Provides text-to-image generation using:
- Smart router: Gemini native image generation (preferred) with DashScope Wanx fallback
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from ai_gateway_core.logging import get_logger

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

logger = get_logger(__name__)


# =============================================================================
# Image Generation Tool Definition
# =============================================================================

IMAGE_GENERATION_DEFINITION = ToolDefinition(
    name="generate_image",
    description="Generate images from text descriptions using AI. "
    "Creates high-quality images based on the provided prompt.",
    parameters=[
        ToolParameter(
            name="prompt",
            type="string",
            description="Detailed description of the image to generate. "
            "Be specific about style, content, colors, composition.",
            required=True,
        ),
        ToolParameter(
            name="negative_prompt",
            type="string",
            description="Things to avoid in the image (e.g., 'blurry, low quality, distorted').",
            required=False,
            default="",
        ),
        ToolParameter(
            name="size",
            type="string",
            description="Image size: '1536*1536', '1024*1024', '720*1280', '1280*720'.",
            required=False,
            default="1536*1536",
            enum=["1536*1536", "1024*1024", "720*1280", "1280*720"],
        ),
        ToolParameter(
            name="style",
            type="string",
            description="Image style preset.",
            required=False,
            default="<auto>",
            enum=[
                "<auto>",
                "<photography>",
                "<portrait>",
                "<3d cartoon>",
                "<anime>",
                "<oil painting>",
                "<watercolor>",
                "<sketch>",
                "<flat illustration>",
            ],
        ),
        ToolParameter(
            name="n",
            type="number",
            description="Number of images to generate (1-4). Default is 1.",
            required=False,
            default=1,
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.MEDIUM,
    when_to_use="Use when the user asks to create, generate, draw, or design an image/picture/artwork. "
    "Good for: illustrations, photos, artwork, logos, concept art, portraits, landscapes, "
    "animals, objects, or any visual content that requires AI image generation.",
    when_not_to_use="Do not use for data visualization or charts (use code execution with matplotlib instead). "
    "Do not use if the user just wants to analyze or describe existing images. "
    "Do not use for mathematical function plots - use code execution instead.",
    examples=[
        ToolExample(
            description="Generate a logo",
            input={
                "prompt": "A modern minimalist logo for a tech company, blue gradient, clean lines",
                "style": "<flat illustration>",
            },
            expected_output="Returns a generated logo image",
        ),
        ToolExample(
            description="Generate an illustration",
            input={
                "prompt": "A cozy coffee shop interior with warm lighting, plants, and wooden furniture",
                "size": "1280*720",
                "style": "<watercolor>",
            },
            expected_output="Returns a watercolor style illustration",
        ),
    ],
    timeout_seconds=120,
)


# =============================================================================
# DashScope Wanx Image Generator
# =============================================================================


@dataclass
class ImageGenerationResult:
    """Result of image generation."""

    success: bool
    images: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{filename, content_base64, mime_type, size_bytes}]
    error: str | None = None
    task_id: str | None = None
    duration_ms: float = 0


class DashScopeImageGenerator:
    """Image generator using DashScope Wanx API.

    Endpoint selection honours ``resolve_dashscope("image")`` so
    operators can set ``DASHSCOPE_IMAGE_API_KEY`` /
    ``DASHSCOPE_IMAGE_BASE_URL`` to swap image gen between the CN
    paid endpoint and the Intl free-trial endpoint independently of
    chat or embedding. Unset → falls back to DASHSCOPE_API_KEY +
    DASHSCOPE_BASE_URL, same as before.
    """

    # ``resolve_dashscope("image")`` returns the native API base ending in
    # ``/api/v1``. Keep only paths relative to that base here so the raw HTTP
    # client and native SDK consumers share one resolver contract.
    _SUBMIT_PATH = "/services/aigc/text2image/image-synthesis"
    _TASK_PATH_TPL = "/tasks/{task_id}"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        from ai_gateway_core.config import resolve_dashscope

        if api_key:
            self.api_key = api_key
            self.base_url = "https://dashscope.aliyuncs.com/api/v1"
        else:
            resolved_key, resolved_base = resolve_dashscope("image")
            self.api_key = resolved_key or None
            self.base_url = resolved_base.rstrip("/")
        self.model = model or os.getenv("DASHSCOPE_IMAGE_MODEL", "wanx-v1")
        self._client: httpx.AsyncClient | None = None

    @property
    def SUBMIT_URL(self) -> str:  # noqa: N802 — kept for backwards-compat
        return f"{self.base_url}{self._SUBMIT_PATH}"

    @property
    def TASK_URL(self) -> str:  # noqa: N802 — kept for backwards-compat
        return f"{self.base_url}{self._TASK_PATH_TPL}"

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
        negative_prompt: str = "",
        size: str = "1536*1536",
        style: str = "<auto>",
        n: int = 1,
        model_override: str | None = None,
    ) -> ImageGenerationResult:
        """Generate images from text prompt."""
        if not self.is_configured:
            return ImageGenerationResult(success=False, error="DashScope API key not configured")

        start_time = time.time()
        use_model = model_override or self.model

        try:
            client = await self._get_client()

            # Submit task
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            }

            payload = {
                "model": use_model,
                "input": {
                    "prompt": prompt,
                },
                "parameters": {
                    "size": size,
                    "n": min(n, 4),
                    "style": style,
                },
            }

            if negative_prompt:
                payload["input"]["negative_prompt"] = negative_prompt

            logger.info(f"Submitting image generation task: {prompt[:50]}...")

            response = await client.post(
                self.SUBMIT_URL,
                headers=headers,
                json=payload,
            )

            if response.status_code != 200:
                error_text = response.text
                logger.error(f"Image generation submit failed: {error_text}")
                return ImageGenerationResult(
                    success=False, error=f"API error: {response.status_code} - {error_text}"
                )

            result = response.json()
            task_id = result.get("output", {}).get("task_id")

            if not task_id:
                return ImageGenerationResult(success=False, error="No task_id returned from API")

            logger.info(f"Image generation task submitted: {task_id}")

            # Poll for completion
            images = await self._poll_task(client, headers, task_id)

            duration_ms = (time.time() - start_time) * 1000

            return ImageGenerationResult(
                success=True,
                images=images,
                task_id=task_id,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.error(f"Image generation failed: {e}")
            return ImageGenerationResult(
                success=False,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000,
            )

    async def _poll_task(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        task_id: str,
        max_wait: int = 120,
        poll_interval: float = 2.0,
    ) -> list[dict[str, Any]]:
        """Poll task until completion."""
        url = self.TASK_URL.format(task_id=task_id)

        elapsed = 0
        while elapsed < max_wait:
            response = await client.get(url, headers=headers)

            if response.status_code != 200:
                raise Exception(f"Task poll failed: {response.status_code}")

            result = response.json()
            output = result.get("output", {})
            task_status = output.get("task_status")

            logger.debug(f"Task {task_id} status: {task_status}")

            if task_status == "SUCCEEDED":
                return await self._download_images(client, output.get("results", []))
            elif task_status == "FAILED":
                raise Exception(f"Task failed: {output.get('message', 'Unknown error')}")
            elif task_status in ("PENDING", "RUNNING"):
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
            else:
                raise Exception(f"Unknown task status: {task_status}")

        raise Exception(f"Task timed out after {max_wait}s")

    async def _download_images(
        self,
        client: httpx.AsyncClient,
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Download images from URLs and convert to base64."""
        images = []

        for i, result in enumerate(results):
            url = result.get("url")
            if not url:
                continue

            try:
                response = await client.get(url)
                if response.status_code == 200:
                    content = response.content

                    # Detect actual MIME type from response headers or content
                    content_type = response.headers.get("content-type", "image/png")
                    # Strip charset and other params
                    mime_type = content_type.split(";")[0].strip()

                    # Determine extension from mime type
                    ext_map = {
                        "image/png": "png",
                        "image/jpeg": "jpg",
                        "image/jpg": "jpg",
                        "image/webp": "webp",
                        "image/gif": "gif",
                    }
                    ext = ext_map.get(mime_type, "png")

                    logger.info(f"Downloaded image {i + 1}: {len(content)} bytes, type={mime_type}")

                    images.append(
                        {
                            "filename": f"generated_image_{i + 1}.{ext}",
                            "content_base64": base64.b64encode(content).decode("utf-8"),
                            "mime_type": mime_type,
                            "size_bytes": len(content),
                        }
                    )
            except Exception as e:
                logger.warning(f"Failed to download image {i}: {e}")

        return images

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# =============================================================================
# Image Generation Tool Executor
# =============================================================================


class ImageGeneratorExecutor(ToolExecutor):
    """Executor for image generation tool."""

    @staticmethod
    def _size_to_aspect_ratio(size: str) -> str:
        """Map DashScope size (e.g. 1024*1024) to Gemini aspectRatio (e.g. 1:1)."""
        try:
            raw = (size or "").lower()
            parts = raw.split("*")
            if len(parts) != 2:
                return "1:1"
            w = float(parts[0])
            h = float(parts[1])
            if w <= 0 or h <= 0:
                return "1:1"
            ratio = w / h
        except Exception:
            return "1:1"

        candidates = {
            "1:1": 1.0,
            "16:9": 16 / 9,
            "9:16": 9 / 16,
            "4:3": 4 / 3,
            "3:4": 3 / 4,
        }
        return min(candidates.keys(), key=lambda k: abs(ratio - candidates[k]))

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        """Execute image generation."""
        prompt = request.arguments.get("prompt", "")
        negative_prompt = request.arguments.get("negative_prompt", "")
        size = request.arguments.get("size", "1536*1536")
        style = request.arguments.get("style", "<auto>")
        n = request.arguments.get("n", 1)

        if not prompt:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Prompt is required",
            )

        from .gemini_image_tool import get_gemini_image_generator
        from .smart_image_generator import get_smart_image_generator

        gemini = get_gemini_image_generator()
        dash = get_image_generator()
        if not gemini.is_configured and not dash.is_configured:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Image generation not configured (missing GEMINI_API_KEY/GOOGLE_API_KEY and DASHSCOPE_API_KEY)",
            )

        aspect_ratio = self._size_to_aspect_ratio(size)

        # Determine provider preference from metadata (model_id → provider)
        from .image_helpers import resolve_image_routing

        model_id = request.metadata.get("model_id", "")
        selected_provider = None
        registry = request.metadata.get("model_registry")
        if model_id and registry is not None:
            try:
                info = registry.get_model(model_id)
                if info:
                    selected_provider = info.provider.value
            except Exception:
                selected_provider = None

        prefer_gemini, prefer_doubao, dashscope_model = resolve_image_routing(
            model_id, selected_provider,
        )

        router = get_smart_image_generator()
        res = await router.generate(
            prompt=prompt,
            n=n,
            size=size,
            style=style,
            negative_prompt=negative_prompt,
            aspect_ratio=aspect_ratio,
            prefer_gemini=prefer_gemini,
            prefer_doubao=prefer_doubao,
            dashscope_model=dashscope_model,
        )

        if not res.success:
            # Preserve safety block signal for the agent
            err = res.error or "Image generation failed"
            if res.blocked and res.block_reason:
                err = f"{err} (blocked: {res.block_reason})"
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=err,
                metadata={
                    "provider": res.provider,
                    "blocked": res.blocked,
                    "block_reason": res.block_reason,
                    "error_code": res.error_code,
                    "duration_ms": res.duration_ms,
                    "used_fallback": res.used_fallback,
                },
            )

        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result=f"Successfully generated {len(res.images)} image(s) for: {prompt[:100]}",
            output_files=res.images,
            metadata={
                "provider": res.provider,
                "duration_ms": res.duration_ms,
                "image_count": len(res.images),
                "prompt": prompt,
                "size": size,
                "style": style,
                "used_fallback": res.used_fallback,
            },
        )


# =============================================================================
# Registration Helper
# =============================================================================

_image_generator: DashScopeImageGenerator | None = None


def get_image_generator() -> DashScopeImageGenerator:
    """Get or create the global image generator instance."""
    global _image_generator
    if _image_generator is None:
        _image_generator = DashScopeImageGenerator()
    return _image_generator


def register_image_generation_tool() -> bool:
    """Register image generation tool with the global registry."""
    from .gemini_image_tool import get_gemini_image_generator

    dash = get_image_generator()
    gemini = get_gemini_image_generator()

    if not dash.is_configured and not gemini.is_configured:
        logger.warning(
            "No image generation provider configured; tool not registered "
            "(missing GEMINI_API_KEY/GOOGLE_API_KEY and DASHSCOPE_API_KEY)"
        )
        return False

    register_tool(IMAGE_GENERATION_DEFINITION, ImageGeneratorExecutor())
    logger.info(
        "Registered image generation tool (gemini=%s, dashscope=%s)",
        gemini.is_configured,
        dash.is_configured,
    )
    return True
