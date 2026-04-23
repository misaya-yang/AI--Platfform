from __future__ import annotations

import pytest


def _item(dua_id: str = "d1", occasion: str = "Morning") -> dict:
    return {
        "dua_id": dua_id,
        "category": "daily",
        "title": "Morning dua",
        "arabic_text": "اللَّهُمَّ",
        "transliteration": "Allahumma",
        "english_meaning": "O Allah",
        "urdu_meaning": "",
        "source": "Hisn",
        "reference": "",
        "authenticity": "sahih",
        "occasion": occasion,
        "data_source": "seed",
        "verification_status": "verified",
    }


class StubDuaQueryService:
    async def get_categories(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_categories",
            "source": "kaggle/islamic-dua-adhkar",
            "total_categories": 1,
            "total_duas": 1,
            "categories": [{"category": "daily", "dua_count": 1}],
        }

    async def get_items_by_category(self, category: str):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_category_items",
            "source": "kaggle/islamic-dua-adhkar",
            "category": category,
            "total": 1,
            "items": [_item()],
        }

    async def get_all_items(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_all_items",
            "source": "kaggle/islamic-dua-adhkar",
            "total": 1,
            "items": [_item()],
        }

    async def get_detail(self, dua_id: str):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_detail",
            "source": "kaggle/islamic-dua-adhkar",
            "dua": _item(dua_id=dua_id),
        }

    async def search_duas(self, query: str, *, limit=20, offset=0):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_search",
            "source": "kaggle/islamic-dua-adhkar",
            "query": query,
            "total": 1,
            "limit": limit,
            "offset": offset,
            "items": [_item()],
        }

    async def get_items_by_occasion(self, occasion: str):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_by_occasion",
            "source": "kaggle/islamic-dua-adhkar",
            "occasion": occasion,
            "total": 1,
            "items": [_item(occasion=occasion)],
        }

    async def get_random(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_random",
            "source": "kaggle/islamic-dua-adhkar",
            "dua": _item(),
        }

    async def list_occasions(self):
        return {
            "generated_at": "2026-04-23T00:00:00Z",
            "screen": "dua_occasions",
            "source": "kaggle/islamic-dua-adhkar",
            "total_occasions": 2,
            "occasions": [
                {"occasion": "Morning", "dua_count": 5},
                {"occasion": "Before eating", "dua_count": 2},
            ],
        }


@pytest.mark.asyncio
async def test_dua_endpoints(async_client, test_app):
    test_app.state.dua_query_service = StubDuaQueryService()

    categories = await async_client.get("/api/v1/dua/categories")
    cat_items = await async_client.get("/api/v1/dua/categories/daily")
    all_items = await async_client.get("/api/v1/dua/items")
    detail = await async_client.get("/api/v1/dua/d1")
    search = await async_client.get("/api/v1/dua/search", params={"q": "Allah"})
    occasions = await async_client.get("/api/v1/dua/occasions")
    by_occasion = await async_client.get("/api/v1/dua/by-occasion/Morning")
    random = await async_client.get("/api/v1/dua/random")

    assert categories.status_code == 200
    assert categories.json()["categories"][0]["category"] == "daily"
    assert cat_items.json()["items"][0]["dua_id"] == "d1"
    assert all_items.json()["items"][0]["dua_id"] == "d1"
    assert detail.json()["dua"]["dua_id"] == "d1"
    assert search.status_code == 200
    assert search.json()["query"] == "Allah"
    assert search.json()["items"][0]["title"] == "Morning dua"
    assert occasions.json()["total_occasions"] == 2
    assert occasions.json()["occasions"][0]["occasion"] == "Morning"
    assert by_occasion.json()["occasion"] == "Morning"
    assert by_occasion.json()["items"][0]["occasion"] == "Morning"
    assert random.status_code == 200
    assert random.json()["dua"]["dua_id"] == "d1"
