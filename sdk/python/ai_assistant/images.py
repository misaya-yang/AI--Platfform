"""
Image generation module.

Wraps the gateway's smart-routing image generation endpoint which
automatically routes to the appropriate provider (DashScope Wanx,
Gemini native, etc.) based on the selected model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ai_assistant.models.request import ImageGenerationRequest
from ai_assistant.models.response import ImageResult

if TYPE_CHECKING:
    from ai_assistant.transport.http import HTTPTransport


class ImageModule:
    """High-level image generation operations."""

    _GENERATE_PATH = "/api/v1/assistant/generate-image"
    _TASK_PATH = "/api/v1/assistant/tasks/{task_id}"
    _CANCEL_PATH = "/api/v1/assistant/tasks/{task_id}/cancel"

    def __init__(self, transport: HTTPTransport) -> None:
        self._transport = transport

    async def generate(
        self,
        prompt: str,
        *,
        model_id: str | None = None,
        style: str | None = None,
        size: str | None = None,
        n: int = 1,
    ) -> ImageResult:
        """Generate images synchronously.

        Blocks until the server returns the generated image URLs.

        Args:
            prompt: Text description of the desired image.
            model_id: Model identifier (determines provider routing).
            style: Optional style preset.
            size: Optional size string, e.g. ``"1024x1024"``.
            n: Number of images to generate.

        Returns:
            ``ImageResult`` containing the image URLs and metadata.
        """
        req = ImageGenerationRequest(
            prompt=prompt,
            model_id=model_id,
            style=style,
            size=size,
            n=n,
        )
        resp = await self._transport.request("POST", self._GENERATE_PATH, json=req.to_dict())
        return ImageResult.from_dict(resp.json())

    async def generate_async(
        self,
        prompt: str,
        *,
        model_id: str | None = None,
        style: str | None = None,
        size: str | None = None,
        n: int = 1,
    ) -> dict[str, Any]:
        """Submit an image generation request asynchronously.

        Returns immediately with a task ID that can be polled via
        ``get_task()``.

        Returns:
            Dict with ``task_id`` and initial status.
        """
        req = ImageGenerationRequest(
            prompt=prompt,
            model_id=model_id,
            style=style,
            size=size,
            n=n,
        )
        body = req.to_dict()
        body["async"] = True
        resp = await self._transport.request("POST", self._GENERATE_PATH, json=body)
        return resp.json()

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Poll the status of an async image generation task.

        Returns:
            Dict with current task status and, if complete, image URLs.
        """
        path = self._TASK_PATH.format(task_id=task_id)
        resp = await self._transport.request("GET", path)
        return resp.json()

    async def cancel(self, task_id: str, *, reason: str | None = None) -> dict[str, Any]:
        """Cancel a running image generation task.

        Args:
            task_id: The task identifier.
            reason: Optional cancellation reason.

        Returns:
            Cancellation status dict.
        """
        path = self._CANCEL_PATH.format(task_id=task_id)
        body: dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        resp = await self._transport.request("POST", path, json=body)
        return resp.json()
