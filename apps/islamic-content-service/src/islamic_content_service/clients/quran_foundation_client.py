from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import QuranSettings
from ..domain.errors import UpstreamAPIError


class QuranFoundationClient:
    def __init__(
        self,
        settings: QuranSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Accept": "application/json", "User-Agent": "islamic-content-service/0.1"},
        )
        self._owns_client = client is None
        self._token_lock = asyncio.Lock()
        self._token: str | None = settings.access_token.strip() or None
        self._token_expires_at: datetime | None = None

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    def is_configured(self) -> bool:
        return bool(
            self.settings.client_id.strip()
            and (self.settings.client_secret.strip() or self.settings.access_token.strip())
        )

    async def _fetch_access_token(self) -> str:
        url = f"{self.settings.auth_url.rstrip('/')}/oauth2/token"
        response = await self._client.post(
            url,
            auth=(self.settings.client_id, self.settings.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": self.settings.scope},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UpstreamAPIError(
                f"Quran OAuth token request failed ({exc.response.status_code}): {exc.response.text[:500]}"
            ) from exc
        payload = response.json()
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise UpstreamAPIError("Quran OAuth response did not include access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        self._token = token
        self._token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(expires_in - 60, 60)
        )
        return token

    async def _get_access_token(self) -> str:
        if self.settings.access_token.strip():
            return self.settings.access_token.strip()
        if (
            self._token
            and self._token_expires_at
            and datetime.now(timezone.utc) < self._token_expires_at
        ):
            return self._token
        async with self._token_lock:
            if (
                self._token
                and self._token_expires_at
                and datetime.now(timezone.utc) < self._token_expires_at
            ):
                return self._token
            return await self._fetch_access_token()

    async def _headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        if not self.settings.client_id.strip() or not token:
            raise UpstreamAPIError("Quran client is missing client_id or access token")
        return {"x-client-id": self.settings.client_id.strip(), "x-auth-token": token}

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = await self._client.get(url, params=params, headers=await self._headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and retry_on_401 and not self.settings.access_token:
                self._token = None
                self._token_expires_at = None
                return await self._request_json(path, params=params, retry_on_401=False)
            raise UpstreamAPIError(
                f"Quran Foundation request failed ({exc.response.status_code}) for {url}: {exc.response.text[:500]}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamAPIError(f"Unexpected Quran Foundation response shape for {url}")
        return payload

    async def get_chapters(self) -> dict[str, Any]:
        return await self._request_json("chapters")

    async def get_translations(self) -> dict[str, Any]:
        return await self._request_json("resources/translations", params={"language": "en"})

    async def get_recitations(self) -> dict[str, Any]:
        return await self._request_json("resources/recitations")

    async def get_chapter_verses(
        self,
        chapter_id: int,
        *,
        translation_ids: list[int] | None = None,
        recitation_id: int | None = None,
        word_fields: str,
        page: int,
        per_page: int,
        include_words: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "language": "en",
            "fields": "text_uthmani",
            "per_page": per_page,
            "page": page,
        }
        if include_words:
            params["words"] = "true"
            params["word_fields"] = word_fields
        if translation_ids:
            params["translations"] = ",".join(str(item) for item in translation_ids)
        if recitation_id is not None:
            params["audio"] = recitation_id
        return await self._request_json(
            f"verses/by_chapter/{chapter_id}",
            params=params,
        )

    async def get_verse(
        self,
        verse_key: str,
        *,
        translation_ids: list[int] | None = None,
        recitation_id: int | None = None,
        word_fields: str,
        include_words: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "language": "en",
            "fields": "text_uthmani",
        }
        if include_words:
            params["words"] = "true"
            params["word_fields"] = word_fields
        if translation_ids:
            params["translations"] = ",".join(str(item) for item in translation_ids)
        if recitation_id is not None:
            params["audio"] = recitation_id
        return await self._request_json(
            f"verses/by_key/{verse_key}",
            params=params,
        )

    async def get_chapter_audio(
        self,
        chapter_id: int,
        *,
        recitation_id: int,
        segments: bool = False,
    ) -> dict[str, Any]:
        return await self._request_json(
            f"chapter_recitations/{recitation_id}/{chapter_id}",
            params={"segments": "true" if segments else "false"},
        )
