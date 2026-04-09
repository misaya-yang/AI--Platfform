"""Image generation callback service.

Sends completed image generation results to an external callback URL.
Used by downstream services (e.g. Wahda app) that prefer push over polling.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CALLBACK_TIMEOUT = 10  # seconds


async def send_image_callback(callback_url: str, task: dict[str, Any]) -> bool:
    """POST completed task data to the callback URL.

    Args:
        callback_url: The external endpoint to notify.
        task: The task dict from _image_tasks.

    Returns:
        True if callback succeeded (code=0), False otherwise.
    """
    images = []
    for img in task.get("images", []):
        images.append({
            "artifact_id": img.get("artifact_id"),
            "download_url": img.get("download_url"),
            "url": img.get("url", ""),
            "width": img.get("width"),
            "height": img.get("height"),
        })

    payload = {
        "task_id": task.get("task_id"),
        "status": task.get("status", "completed"),
        "progress": task.get("progress", 100),
        "prompt": task.get("prompt", ""),
        "model_id": task.get("model_id", ""),
        "provider": task.get("provider", ""),
        "images": images,
        "duration_ms": task.get("duration_ms"),
        "error": task.get("error"),
        "created_at": task.get("created_at"),
        "completed_at": task.get("completed_at"),
    }

    try:
        async with httpx.AsyncClient(timeout=_CALLBACK_TIMEOUT) as client:
            resp = await client.post(callback_url, json=payload)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("code") == 0:
                    logger.info("[ImageCallback] Success → %s (task=%s)", callback_url, task.get("task_id"))
                    return True
                else:
                    logger.warning("[ImageCallback] Non-zero code from %s: %s", callback_url, body)
                    return False
            else:
                logger.warning("[ImageCallback] HTTP %d from %s", resp.status_code, callback_url)
                return False
    except Exception as e:
        logger.error("[ImageCallback] Failed to send to %s: %s", callback_url, e)
        return False
