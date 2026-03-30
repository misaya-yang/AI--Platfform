from __future__ import annotations

import httpx
import pytest

from islamic_content_service.clients.hadith_cdn_client import HadithCdnClient
from islamic_content_service.config import HadithSettings


@pytest.mark.asyncio
async def test_hadith_cdn_client_falls_back_from_minified_to_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".min.json"):
            return httpx.Response(404, json={"detail": "missing"})
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json={"metadata": {"name": "Sahih al Bukhari"}, "hadiths": []})
        return httpx.Response(500, json={"detail": "unexpected"})

    transport = httpx.MockTransport(handler)
    settings = HadithSettings(base_url="https://cdn.example.test")
    client = HadithCdnClient(settings, client=httpx.AsyncClient(transport=transport))

    payload = await client.get_edition("eng-bukhari")

    assert payload["metadata"]["name"] == "Sahih al Bukhari"


@pytest.mark.asyncio
async def test_hadith_cdn_client_reads_editions_catalog():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bukhari": {"name": "Sahih al Bukhari"}})

    transport = httpx.MockTransport(handler)
    settings = HadithSettings(base_url="https://cdn.example.test", prefer_minified=False)
    client = HadithCdnClient(settings, client=httpx.AsyncClient(transport=transport))

    payload = await client.get_editions()

    assert payload["bukhari"]["name"] == "Sahih al Bukhari"
