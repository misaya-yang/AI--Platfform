from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import HadithSettings
from ..domain.errors import UpstreamAPIError

logger = logging.getLogger(__name__)


class HadithCdnClient:
    def __init__(self, settings: HadithSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "islamic-content-service/0.1"},
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def is_configured(self) -> bool:
        return bool(self.settings.base_url.strip())

    async def _request_json(self, path: str) -> dict[str, Any]:
        base = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        for suffix in (".min.json", ".json") if self.settings.prefer_minified else (".json", ".min.json"):
            url = f"{base}{suffix}"
            response = await self._client.get(url)
            if response.status_code == 404:
                logger.warning("Hadith CDN resource missing at %s, trying fallback", url)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise UpstreamAPIError(
                    f"Hadith CDN request failed ({exc.response.status_code}) for {url}: {exc.response.text[:500]}"
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise UpstreamAPIError(f"Unexpected Hadith CDN response shape for {url}")
            return payload
        raise UpstreamAPIError(f"Hadith CDN resource not found for {base}")

    async def get_editions(self) -> dict[str, Any]:
        return await self._request_json("editions")

    async def get_edition(self, edition_name: str) -> dict[str, Any]:
        return await self._request_json(f"editions/{edition_name}")
