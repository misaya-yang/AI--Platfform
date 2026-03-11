from __future__ import annotations

from typing import Any

import httpx

from ..config import HadithSettings
from ..domain.errors import UpstreamAPIError


class SunnahClient:
    def __init__(self, settings: HadithSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Accept": "application/json", "User-Agent": "islamic-content-service/0.1"},
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def is_configured(self) -> bool:
        return bool(self.settings.api_key.strip())

    def _headers(self) -> dict[str, str]:
        if not self.settings.api_key.strip():
            raise UpstreamAPIError("Sunnah API key is not configured")
        return {"X-API-Key": self.settings.api_key.strip()}

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = await self._client.get(url, params=params, headers=self._headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UpstreamAPIError(
                f"Sunnah request failed ({exc.response.status_code}) for {url}: {exc.response.text[:500]}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamAPIError(f"Unexpected Sunnah response shape for {url}")
        return payload

    async def get_collections(self, *, page: int, limit: int) -> dict[str, Any]:
        return await self._request_json("collections", params={"page": page, "limit": limit})

    async def get_collection(self, collection_name: str) -> dict[str, Any]:
        return await self._request_json(f"collections/{collection_name}")

    async def get_books(self, collection_name: str, *, page: int, limit: int) -> dict[str, Any]:
        return await self._request_json(
            f"collections/{collection_name}/books",
            params={"page": page, "limit": limit},
        )

    async def get_book_hadiths(
        self,
        collection_name: str,
        book_number: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        return await self._request_json(
            f"collections/{collection_name}/books/{book_number}/hadiths",
            params={"page": page, "limit": limit},
        )

    async def get_hadith_detail(self, collection_name: str, hadith_number: str) -> dict[str, Any]:
        return await self._request_json(f"collections/{collection_name}/hadiths/{hadith_number}")
