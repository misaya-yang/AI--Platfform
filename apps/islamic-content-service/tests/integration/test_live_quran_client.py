from __future__ import annotations

import os

import pytest

from islamic_content_service.clients.quran_foundation_client import QuranFoundationClient
from islamic_content_service.config import QuranSettings


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_quran_client_fetches_chapters():
    client_id = os.getenv("ISLAMIC_CONTENT_TEST_QURAN_CLIENT_ID")
    client_secret = os.getenv("ISLAMIC_CONTENT_TEST_QURAN_CLIENT_SECRET")
    auth_url = os.getenv("ISLAMIC_CONTENT_TEST_QURAN_AUTH_URL")
    if not client_id or not client_secret or not auth_url:
        pytest.skip("Live Quran credentials not provided")

    client = QuranFoundationClient(
        QuranSettings(
            client_id=client_id,
            client_secret=client_secret,
            auth_url=auth_url,
        )
    )
    try:
        payload = await client.get_chapters()
    finally:
        await client.close()

    assert "chapters" in payload
    assert payload["chapters"]
