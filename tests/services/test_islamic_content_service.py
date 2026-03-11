from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config.settings import IslamicContentSettings
from src.services.islamic_content import IslamicContentService


@pytest.mark.asyncio
async def test_get_quran_triplets_groups_ayahs(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path)),
        client=client,
    )
    service.get_quran_chapter_ayahs = AsyncMock(
        return_value={
            "ayahs": [
                {
                    "verse_key": "1:1",
                    "ayah_number": 1,
                    "arabic_text": "A1",
                    "transliteration_text": "T1",
                    "translation_text": "E1",
                    "audio": {"url": "u1"},
                },
                {
                    "verse_key": "1:2",
                    "ayah_number": 2,
                    "arabic_text": "A2",
                    "transliteration_text": "T2",
                    "translation_text": "E2",
                    "audio": {"url": "u2"},
                },
                {
                    "verse_key": "1:3",
                    "ayah_number": 3,
                    "arabic_text": "A3",
                    "transliteration_text": "T3",
                    "translation_text": "E3",
                    "audio": {"url": "u3"},
                },
                {
                    "verse_key": "1:4",
                    "ayah_number": 4,
                    "arabic_text": "A4",
                    "transliteration_text": "T4",
                    "translation_text": "E4",
                    "audio": {"url": "u4"},
                },
            ]
        }
    )

    payload = await service.get_quran_triplets(1, group_size=3, use_cache=False)

    assert payload["chapter_id"] == 1
    assert len(payload["blocks"]) == 2
    assert payload["blocks"][0]["ref"] == "1:1-3"
    assert payload["blocks"][1]["ref"] == "1:4-4"
    assert payload["blocks"][0]["audio_urls"][0]["url"] == "u1"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_hadith_books_paginates_all_pages(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path), sunnah_api_key="test-key"),
        client=client,
    )
    service._request_json = AsyncMock(
        side_effect=[
            {
                "data": {
                    "name": "bukhari",
                    "collection": [{"lang": "en", "title": "Sahih al-Bukhari"}],
                }
            },
            {
                "data": [
                    {"bookNumber": "1", "book": [{"lang": "en", "name": "Book 1"}]},
                ],
                "pagination": {"totalPages": 2},
            },
            {
                "data": [
                    {"bookNumber": "2", "book": [{"lang": "en", "name": "Book 2"}]},
                ],
                "pagination": {"totalPages": 2},
            },
        ]
    )

    payload = await service.get_hadith_books("bukhari", use_cache=False)

    assert payload["collection"]["name"] == "bukhari"
    assert [item["book_number"] for item in payload["books"]] == ["1", "2"]
    await client.aclose()


@pytest.mark.asyncio
async def test_sync_static_content_fetches_hadith_details(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path), sunnah_api_key="test-key"),
        client=client,
    )
    service.get_hadith_collections = AsyncMock(
        return_value={"collections": [{"name": "bukhari"}]}
    )
    service.get_hadith_books = AsyncMock(
        return_value={"books": [{"book_number": "1"}]}
    )
    service.get_hadith_book_items = AsyncMock(
        return_value={
            "items": [{"hadith_number": "1"}, {"hadith_number": "2"}],
            "pagination": {"totalPages": 1},
        }
    )
    service.get_hadith_detail = AsyncMock(return_value={"hadith": {"hadith_number": "1"}})

    manifest = await service.sync_static_content(
        include_quran=False,
        include_hadith=True,
        include_duas=False,
    )

    assert manifest["steps"][0]["detail_items"] == 2
    assert service.get_hadith_detail.await_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_sync_static_content_persists_canonical_rows(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    repository = AsyncMock()
    repository.enabled = True
    repository.start_sync_run = AsyncMock(return_value="run-1")
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path)),
        client=client,
        repository=repository,
    )
    service.get_quran_chapters = AsyncMock(
        return_value={"chapters": [{"chapter_id": 1, "name_simple": "Al-Fatihah"}]}
    )
    service.get_quran_translations = AsyncMock(return_value={"translations": [{"id": 20}]})
    service.get_quran_recitations = AsyncMock(return_value={"recitations": [{"id": 7}]})
    service.get_quran_chapter_ayahs = AsyncMock(
        return_value={
            "translation_id": 20,
            "recitation_id": 7,
            "ayahs": [
                {
                    "verse_key": "1:1",
                    "ayah_number": 1,
                    "words": [],
                    "audio": {"url": "https://cdn.example/1-1.mp3"},
                }
            ],
        }
    )
    service.get_quran_triplets = AsyncMock(
        return_value={
            "translation_id": 20,
            "recitation_id": 7,
            "blocks": [
                {
                    "block_id": "quran:1:1-1",
                    "verse_keys": ["1:1"],
                    "arabic_text": "A1",
                    "transliteration_text": "T1",
                    "translation_text": "E1",
                    "audio_urls": [{"verse_key": "1:1", "url": "https://cdn.example/1-1.mp3"}],
                }
            ],
        }
    )

    manifest = await service.sync_static_content(
        include_quran=True,
        include_hadith=False,
        include_duas=False,
        persist_db=True,
    )

    assert manifest["steps"][0]["persisted_to_db"] is True
    repository.upsert_quran_chapters.assert_awaited_once()
    repository.upsert_quran_translations.assert_awaited_once()
    repository.upsert_quran_recitations.assert_awaited_once()
    repository.upsert_quran_ayahs.assert_awaited_once()
    repository.upsert_quran_triplets.assert_awaited_once()
    repository.finish_sync_run.assert_awaited_once()
    await client.aclose()


@pytest.mark.asyncio
async def test_get_quran_chapter_translations_returns_lightweight_payload(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path)),
        client=client,
    )
    service.get_quran_chapter_ayahs = AsyncMock(
        return_value={
            "ayahs": [
                {
                    "verse_key": "2:1",
                    "surah_number": 2,
                    "ayah_number": 1,
                    "translation_text": "Alif, Lam, Meem.",
                }
            ]
        }
    )

    payload = await service.get_quran_chapter_translations(2, translation_id=20, use_cache=False)

    assert payload["chapter_id"] == 2
    assert payload["items"][0]["verse_key"] == "2:1"
    assert payload["items"][0]["translation_text"] == "Alif, Lam, Meem."
    await client.aclose()


@pytest.mark.asyncio
async def test_quran_oauth_fetches_token_with_client_credentials(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            assert request.method == "POST"
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "scope=content" in body
            return httpx.Response(200, json={"access_token": "token-123", "expires_in": 3600})
        return httpx.Response(200, json={"chapters": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = IslamicContentService(
        IslamicContentSettings(
            cache_dir=str(tmp_path),
            quran_client_id="client-id",
            quran_client_secret="client-secret",
            quran_auth_url="https://oauth2.quran.foundation",
        ),
        client=client,
    )

    headers = await service._quran_headers()

    assert headers == {"x-client-id": "client-id", "x-auth-token": "token-123"}
    await client.aclose()


def test_normalize_quran_word_includes_audio_and_clean_text(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path)),
        client=client,
    )

    word = service._normalize_quran_word(
        {
            "position": 1,
            "text_uthmani": "بِسْمِ",
            "text_uthmani_simple": "بسم",
            "transliteration": {"text": "<i>bismi</i>"},
            "translation": {"text": "In&nbsp;the name"},
            "audio_url": "https://cdn.example/word.mp3",
            "char_type_name": "word",
        }
    )

    assert word["transliteration"] == "bismi"
    assert word["translation"] == "In the name"
    assert word["audio_url"] == "https://cdn.example/word.mp3"
    asyncio.run(client.aclose())


def test_load_dua_items_from_json(tmp_path: Path):
    dua_file = tmp_path / "duas.json"
    dua_file.write_text(
        json.dumps(
            [
                {
                    "id": "dua-1",
                    "title": "Morning Dua",
                    "category": "Morning Adhkar",
                    "arabic": "abc",
                    "translation": "Morning text",
                }
            ]
        ),
        encoding="utf-8",
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path), duas_file_path=str(dua_file)),
        client=client,
    )

    items = service._load_dua_items()

    assert len(items) == 1
    assert items[0]["dua_id"] == "dua-1"
    assert items[0]["category"] == "Morning Adhkar"
    assert items[0]["translation_text"] == "Morning text"
    asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_get_canonical_summary_uses_repository(tmp_path: Path):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    repository = AsyncMock()
    repository.get_canonical_summary = AsyncMock(
        return_value={
            "database_enabled": True,
            "generated_at": "2026-03-09T00:00:00Z",
            "counts": {"quran_ayahs": 6236},
        }
    )
    service = IslamicContentService(
        IslamicContentSettings(cache_dir=str(tmp_path)),
        client=client,
        repository=repository,
    )

    payload = await service.get_canonical_summary()

    assert payload["database_enabled"] is True
    assert payload["counts"]["quran_ayahs"] == 6236
    repository.get_canonical_summary.assert_awaited_once()
    await client.aclose()
