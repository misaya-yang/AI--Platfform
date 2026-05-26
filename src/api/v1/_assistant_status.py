from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import httpx

ASSISTANT_SERVICE_URL = os.getenv("ASSISTANT_SERVICE_URL", "http://assistant-service:8093").rstrip("/")


async def get_assistant_health(timeout: float = 2.0) -> dict[str, Any]:
    started = time.perf_counter()
    checked_at = datetime.utcnow().isoformat()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{ASSISTANT_SERVICE_URL}/health")
        latency = time.perf_counter() - started
        if response.status_code >= 400:
            return {
                "status": "unavailable",
                "latency": latency,
                "last_check": checked_at,
                "error": f"assistant-service health returned HTTP {response.status_code}",
            }
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raw_status = str(payload.get("status") or "").lower()
        status = "healthy" if raw_status in {"", "ok", "healthy"} else raw_status
        return {
            "status": status,
            "latency": latency,
            "last_check": checked_at,
            "error": None if status == "healthy" else payload.get("error") or raw_status,
        }
    except httpx.HTTPError as exc:
        return {
            "status": "unavailable",
            "latency": None,
            "last_check": checked_at,
            "error": str(exc),
        }
