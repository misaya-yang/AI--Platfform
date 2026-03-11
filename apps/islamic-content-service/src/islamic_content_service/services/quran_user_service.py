from __future__ import annotations

from datetime import datetime, timezone

from ..clients.quran_user_client import QuranUserClient
from ..config import QuranUserSettings
from ..domain.errors import NotReadyError


class QuranUserService:
    def __init__(self, settings: QuranUserSettings, client: QuranUserClient) -> None:
        self.settings = settings
        self.client = client

    def ensure_configured(self) -> None:
        if not self.client.is_configured():
            raise NotReadyError("Quran user OAuth is not configured yet")

    async def get_auth_config(self) -> dict:
        self.ensure_configured()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "enabled": True,
            "auth_url": self.settings.auth_url,
            "api_base_url": self.settings.user_api_base_url,
            "redirect_uri": self.settings.redirect_uri or None,
            "post_logout_redirect_uri": self.settings.post_logout_redirect_uri or None,
            "scopes": self.settings.scopes,
        }

    async def get_authorize_url(
        self,
        *,
        redirect_uri: str | None,
        state: str | None,
        code_challenge: str | None,
        code_challenge_method: str,
        scopes: list[str] | None,
    ) -> dict:
        self.ensure_configured()
        resolved_redirect_uri = (redirect_uri or self.settings.redirect_uri).strip()
        if not resolved_redirect_uri:
            raise NotReadyError("Quran user redirect_uri is required for authorization")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "authorize_url": self.client.build_authorize_url(
                redirect_uri=resolved_redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scopes=scopes,
            ),
            "redirect_uri": resolved_redirect_uri,
            "scopes": scopes or self.settings.scopes,
        }

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str | None,
        code_verifier: str | None,
    ) -> dict:
        self.ensure_configured()
        resolved_redirect_uri = (redirect_uri or self.settings.redirect_uri).strip()
        if not resolved_redirect_uri:
            raise NotReadyError("Quran user redirect_uri is required for token exchange")
        payload = await self.client.exchange_code(
            code=code,
            redirect_uri=resolved_redirect_uri,
            code_verifier=code_verifier,
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "token": payload,
        }

    async def refresh_token(self, refresh_token: str) -> dict:
        self.ensure_configured()
        payload = await self.client.refresh_token(refresh_token)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "token": payload,
        }

    async def get_userinfo(self, access_token: str) -> dict:
        self.ensure_configured()
        payload = await self.client.get_userinfo(access_token)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "userinfo": payload,
        }

    async def proxy_request(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        query: dict | None,
        body: dict | None,
    ) -> dict:
        self.ensure_configured()
        payload = await self.client.request_user_api(
            method=method,
            path=path,
            access_token=access_token,
            query=query,
            body=body,
        )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "method": method.upper(),
            "path": path,
            "payload": payload,
        }
