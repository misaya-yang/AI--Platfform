from __future__ import annotations

from urllib.parse import urlencode

import httpx

from ..config import QuranUserSettings
from ..domain.errors import UpstreamAPIError


class QuranUserClient:
    def __init__(
        self,
        settings: QuranUserSettings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
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
        return bool(
            self.settings.enabled
            and self.settings.client_id.strip()
            and self.settings.auth_url.strip()
            and self.settings.user_api_base_url.strip()
        )

    def build_authorize_url(
        self,
        *,
        redirect_uri: str,
        state: str | None = None,
        code_challenge: str | None = None,
        code_challenge_method: str = "S256",
        scopes: list[str] | None = None,
    ) -> str:
        query = {
            "client_id": self.settings.client_id.strip(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes or self.settings.scopes),
        }
        if state:
            query["state"] = state
        if code_challenge:
            query["code_challenge"] = code_challenge
            query["code_challenge_method"] = code_challenge_method
        base = f"{self.settings.auth_url.rstrip('/')}/oauth2/authorize"
        return f"{base}?{urlencode(query)}"

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> dict:
        data = {
            "grant_type": "authorization_code",
            "client_id": self.settings.client_id.strip(),
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if self.settings.client_secret.strip():
            data["client_secret"] = self.settings.client_secret.strip()
        if code_verifier:
            data["code_verifier"] = code_verifier
        response = await self._client.post(
            f"{self.settings.auth_url.rstrip('/')}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )
        return await self._decode_response(response, "Quran user token exchange failed")

    async def refresh_token(self, refresh_token: str) -> dict:
        data = {
            "grant_type": "refresh_token",
            "client_id": self.settings.client_id.strip(),
            "refresh_token": refresh_token,
        }
        if self.settings.client_secret.strip():
            data["client_secret"] = self.settings.client_secret.strip()
        response = await self._client.post(
            f"{self.settings.auth_url.rstrip('/')}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )
        return await self._decode_response(response, "Quran user token refresh failed")

    async def get_userinfo(self, access_token: str) -> dict:
        response = await self._client.get(
            f"{self.settings.auth_url.rstrip('/')}/oauth2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return await self._decode_response(response, "Quran userinfo request failed")

    async def request_user_api(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        query: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        normalized_path = path.strip().lstrip("/")
        response = await self._client.request(
            method.upper(),
            f"{self.settings.user_api_base_url.rstrip('/')}/{normalized_path}",
            params=query,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return await self._decode_response(
            response,
            f"Quran user API request failed for {normalized_path}",
        )

    async def _decode_response(self, response: httpx.Response, message: str) -> dict:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise UpstreamAPIError(
                f"{message} ({exc.response.status_code}): {exc.response.text[:500]}"
            ) from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamAPIError(f"{message}: unexpected response shape")
        return payload
