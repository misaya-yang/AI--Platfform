from __future__ import annotations

import httpx
import pytest

from islamic_content_service.clients.quran_foundation_client import QuranFoundationClient
from islamic_content_service.config import QuranSettings


@pytest.mark.asyncio
async def test_quran_client_fetches_token_and_chapters():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            return httpx.Response(200, json={"access_token": "token-123", "expires_in": 3600})
        assert request.headers["x-client-id"] == "client-id"
        assert request.headers["x-auth-token"] == "token-123"
        return httpx.Response(200, json={"chapters": [{"id": 1, "name_simple": "Al-Fatihah"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    quran_client = QuranFoundationClient(
        QuranSettings(
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://oauth2.quran.foundation",
            base_url="https://apis.quran.foundation/content/api/v4",
        ),
        client=client,
    )

    payload = await quran_client.get_chapters()

    assert payload["chapters"][0]["id"] == 1
    await quran_client.close()


@pytest.mark.asyncio
async def test_quran_client_requests_chapter_audio_with_reciter_first():
    seen_paths: list[str] = []
    seen_queries: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "token-123", "expires_in": 3600})
        seen_paths.append(request.url.path)
        seen_queries.append(request.url.query)
        return httpx.Response(200, json={"audio_file": {"audio_url": "https://example.com/1.mp3"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    quran_client = QuranFoundationClient(
        QuranSettings(
            client_id="client-id",
            client_secret="client-secret",
            auth_url="https://oauth2.quran.foundation",
            base_url="https://apis.quran.foundation/content/api/v4",
        ),
        client=client,
    )

    payload = await quran_client.get_chapter_audio(1, recitation_id=7, segments=True)

    assert payload["audio_file"]["audio_url"] == "https://example.com/1.mp3"
    assert seen_paths == ["/content/api/v4/chapter_recitations/7/1"]
    assert seen_queries == [b"segments=true"]
    await quran_client.close()
