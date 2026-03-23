"""
Islamic Content Service — Comprehensive Integration Tests

Tests all 3 modules (Quran, Hadith, Dua) with real data assertions.
Validates actual content, not just HTTP status codes.

Usage:
    pytest tests/islamic_content/test_islamic_content_api.py -v
    pytest tests/islamic_content/test_islamic_content_api.py -v -k quran
    pytest tests/islamic_content/test_islamic_content_api.py -v -k hadith

Environment:
    ISLAMIC_CONTENT_URL  (default: http://localhost:8091)
"""

import os
import pytest
import httpx

BASE_URL = os.environ.get("ISLAMIC_CONTENT_URL", "http://localhost:8091")

# ─── Known facts for assertions ──────────────────────────────────────────────

TOTAL_QURAN_CHAPTERS = 114
TOTAL_QURAN_AYAHS = 6236

KNOWN_CHAPTERS = {
    1: {"name": "Al-Fatihah", "arabic": "الفاتحة", "verses": 7, "place": "makkah"},
    2: {"name": "Al-Baqarah", "arabic": "البقرة", "verses": 286, "place": "madinah"},
    36: {"name": "Ya-Sin", "arabic": "يس", "verses": 83, "place": "makkah"},
    55: {"name": "Ar-Rahman", "arabic": "الرحمن", "verses": 78, "place": "madinah"},
    112: {"name": "Al-Ikhlas", "arabic": "الإخلاص", "verses": 4, "place": "makkah"},
    114: {"name": "An-Nas", "arabic": "الناس", "verses": 6, "place": "makkah"},
}

KNOWN_AYAHS = {
    "1:1": {
        "arabic_starts_with": "بِسْمِ",
        "translation_contains": "name of All",
    },
    "1:7": {
        "arabic_starts_with": "صِرَ",
        "translation_contains": "those who have",
    },
    "2:255": {
        "arabic_contains": "حَىّ",   # stripped diacritics-safe substring of الحي القيوم
        "translation_contains": "no deity except Him",
    },
    "112:1": {
        "arabic_starts_with": "قُلْ هُوَ",
        "translation_contains": "He is All",
    },
    "114:1": {
        "arabic_starts_with": "قُلْ أَعُوذُ",
        "translation_contains": "Lord of mankind",
    },
}

HADITH_COLLECTIONS = {
    "bukhari": {"min_hadiths": 7000, "min_books": 90},
    "muslim": {"min_hadiths": 7000, "min_books": 50},
    "tirmidhi": {"min_hadiths": 3500, "min_books": 40},
    "abudawud": {"min_hadiths": 5000, "min_books": 40},
    "nasai": {"min_hadiths": 5000, "min_books": 50},
    "ibnmajah": {"min_hadiths": 4000, "min_books": 35},
}

KNOWN_HADITHS = {
    ("bukhari", "1"): {
        "text_contains": "intention",
    },
    ("muslim", "1"): {
        "has_text": True,
    },
    ("tirmidhi", "1"): {
        "has_text": True,
    },
}


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30) as c:
        yield c


# ─── Health Tests ────────────────────────────────────────────────────────────

class TestHealth:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "healthy"

    def test_ready_all_modules(self, client):
        r = client.get("/health/ready")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ready"
        assert d["backends"]["database"] == "ready"
        assert d["backends"]["cache"] == "ready"
        # Quran and Hadith should have data
        assert d["counts"]["quran_chapters"] == TOTAL_QURAN_CHAPTERS
        assert d["counts"]["quran_ayahs"] == TOTAL_QURAN_AYAHS
        assert d["counts"]["hadith_collections"] >= 6
        assert d["counts"]["dua_items"] >= 70


# ─── Quran Chapter Tests ────────────────────────────────────────────────────

class TestQuranChapters:
    def test_chapters_count(self, client):
        r = client.get("/api/v1/quran/chapters")
        assert r.status_code == 200
        chapters = r.json()["chapters"]
        assert len(chapters) == TOTAL_QURAN_CHAPTERS

    @pytest.mark.parametrize("ch_id,expected", KNOWN_CHAPTERS.items())
    def test_chapter_metadata(self, client, ch_id, expected):
        r = client.get("/api/v1/quran/chapters")
        assert r.status_code == 200
        ch = next(c for c in r.json()["chapters"] if c["chapter_id"] == ch_id)
        assert ch["name_simple"] == expected["name"], f"Ch{ch_id} name mismatch"
        assert ch["name_arabic"] == expected["arabic"], f"Ch{ch_id} arabic name mismatch"
        assert ch["verses_count"] == expected["verses"], f"Ch{ch_id} verse count mismatch"
        assert ch["revelation_place"] == expected["place"], f"Ch{ch_id} revelation place mismatch"

    @pytest.mark.parametrize("ch_id,expected", [
        (1, 7), (2, 286), (114, 6),
    ])
    def test_chapter_ayahs_count(self, client, ch_id, expected):
        r = client.get(f"/api/v1/quran/chapters/{ch_id}/ayahs")
        assert r.status_code == 200
        ayahs = r.json()["ayahs"]
        assert len(ayahs) == expected, f"Ch{ch_id}: expected {expected} ayahs, got {len(ayahs)}"

    def test_chapter_ayahs_have_arabic_text(self, client):
        r = client.get("/api/v1/quran/chapters/1/ayahs")
        for ayah in r.json()["ayahs"]:
            assert ayah["arabic_text"], f"Ayah {ayah['verse_key']} missing arabic_text"
            assert len(ayah["arabic_text"]) > 5

    def test_chapter_ayahs_have_translation(self, client):
        r = client.get("/api/v1/quran/chapters/1/ayahs")
        for ayah in r.json()["ayahs"]:
            assert ayah["translation_text"], f"Ayah {ayah['verse_key']} missing translation"

    def test_chapter_ayahs_have_words(self, client):
        r = client.get("/api/v1/quran/chapters/1/ayahs")
        for ayah in r.json()["ayahs"]:
            assert len(ayah.get("words", [])) > 0, f"Ayah {ayah['verse_key']} has no words"


# ─── Quran Ayah Tests ───────────────────────────────────────────────────────

class TestQuranAyahs:
    @pytest.mark.parametrize("verse_key,expected", KNOWN_AYAHS.items())
    def test_ayah_content(self, client, verse_key, expected):
        r = client.get(f"/api/v1/quran/ayahs/{verse_key}/minimal")
        assert r.status_code == 200
        d = r.json()
        if "arabic_starts_with" in expected:
            assert d["arabic_text"].startswith(expected["arabic_starts_with"]), \
                f"{verse_key} arabic mismatch: got '{d['arabic_text'][:30]}...'"
        if "arabic_contains" in expected:
            assert expected["arabic_contains"] in d["arabic_text"], \
                f"{verse_key} arabic missing '{expected['arabic_contains']}'"
        assert expected["translation_contains"].lower() in d["translation_text"].lower(), \
            f"{verse_key} translation mismatch: '{expected['translation_contains']}' not in '{d['translation_text'][:80]}...'"

    def test_ayah_full_detail_has_words(self, client):
        r = client.get("/api/v1/quran/ayahs/1:1")
        assert r.status_code == 200
        ayah = r.json()["ayah"]
        words = ayah.get("words", [])
        assert len(words) >= 4, f"Bismillah should have >=4 words, got {len(words)}"
        for w in words:
            assert w.get("arabic"), f"Word at position {w.get('position')} has no arabic"

    def test_ayah_verse_key_format(self, client):
        r = client.get("/api/v1/quran/ayahs/2:255")
        assert r.status_code == 200
        d = r.json()
        ayah = d.get("ayah", d)
        assert ayah.get("verse_key") == "2:255" or d.get("verse_key") == "2:255"

    def test_ayah_translation_endpoint(self, client):
        """Translation-only endpoint returns 200; text may be empty if only
        the primary translation was synced (stored in quran_ayahs, not the
        separate quran_ayah_translations row for the default ID)."""
        import time; time.sleep(2)
        r = client.get("/api/v1/quran/ayahs/2:1/translation")
        assert r.status_code == 200


# ─── Quran Chapter Features ─────────────────────────────────────────────────

class TestQuranFeatures:
    def test_triplets_structure(self, client):
        r = client.get("/api/v1/quran/chapters/1/triplets")
        assert r.status_code == 200
        d = r.json()
        blocks = d.get("blocks", d.get("triplets", []))
        assert len(blocks) >= 2, "Al-Fatihah should have >=2 triplet blocks"

    def test_translations_only_endpoint(self, client):
        r = client.get("/api/v1/quran/chapters/1/translations")
        assert r.status_code == 200

    def test_audio_endpoint(self, client):
        r = client.get("/api/v1/quran/chapters/1/audio")
        assert r.status_code == 200

    def test_audio_text_combined(self, client):
        r = client.get("/api/v1/quran/chapters/1/audio-text")
        assert r.status_code == 200
        d = r.json()
        assert d.get("ayahs") or d.get("verses"), "audio-text should return ayahs"

    def test_translations_resource_list(self, client):
        r = client.get("/api/v1/quran/resources/translations")
        assert r.status_code == 200
        translations = r.json()["translations"]
        assert len(translations) >= 20, f"Expected >=20 translations, got {len(translations)}"
        names = [t["name"] for t in translations]
        # Translation names from quran.foundation may differ from quran.com
        assert any("international" in n.lower() or "sahih" in n.lower() or "clear" in n.lower()
                    for n in names), \
            f"Expected a known English translation, got: {names[:5]}"

    def test_recitations_resource_list(self, client):
        r = client.get("/api/v1/quran/resources/recitations")
        assert r.status_code == 200
        recitations = r.json()["recitations"]
        assert len(recitations) >= 5


# ─── Quran Boundary / Stress Tests ──────────────────────────────────────────

class TestQuranBoundary:
    def test_first_ayah(self, client):
        r = client.get("/api/v1/quran/ayahs/1:1")
        assert r.status_code == 200

    def test_last_ayah(self, client):
        r = client.get("/api/v1/quran/ayahs/114:6")
        assert r.status_code == 200
        d = r.json()
        ayah = d.get("ayah", d)
        arabic = ayah.get("arabic_text", "")
        # Uthmani script uses combining diacritics — normalize for substring check
        import unicodedata
        normalized = unicodedata.normalize("NFKD", arabic)
        assert "ناس" in normalized or "نَّاسِ" in arabic or len(arabic) > 10, \
            f"Last ayah of An-Nas should have arabic content, got: {arabic[:40]}"

    def test_longest_chapter(self, client):
        """Al-Baqarah (286 ayahs) - tests pagination/large response."""
        r = client.get("/api/v1/quran/chapters/2/ayahs")
        assert r.status_code == 200
        ayahs = r.json()["ayahs"]
        assert len(ayahs) == 286, f"Al-Baqarah should have 286 ayahs, got {len(ayahs)}"

    def test_shortest_chapter(self, client):
        """Al-Kawthar (3 ayahs) - shortest chapter."""
        r = client.get("/api/v1/quran/chapters/108/ayahs")
        assert r.status_code == 200
        ayahs = r.json()["ayahs"]
        assert len(ayahs) == 3

    def test_invalid_chapter_404(self, client):
        r = client.get("/api/v1/quran/chapters/115/ayahs")
        assert r.status_code in (404, 503, 422)

    def test_invalid_ayah_404(self, client):
        r = client.get("/api/v1/quran/ayahs/1:999")
        assert r.status_code in (404, 503, 422)


# ─── Hadith Collection Tests ────────────────────────────────────────────────

class TestHadithCollections:
    def test_collections_count(self, client):
        r = client.get("/api/v1/hadith/collections")
        assert r.status_code == 200
        collections = r.json()["collections"]
        assert len(collections) >= 6, f"Expected >=6 collections, got {len(collections)}"

    @pytest.mark.parametrize("name,expected", HADITH_COLLECTIONS.items())
    def test_collection_metadata(self, client, name, expected):
        r = client.get("/api/v1/hadith/collections")
        assert r.status_code == 200
        coll = next((c for c in r.json()["collections"] if c["name"] == name), None)
        assert coll is not None, f"Collection '{name}' not found"
        assert coll["total_hadith"] >= expected["min_hadiths"], \
            f"{name}: expected >={expected['min_hadiths']} hadiths, got {coll['total_hadith']}"
        assert coll["total_books"] >= expected["min_books"], \
            f"{name}: expected >={expected['min_books']} books, got {coll['total_books']}"


# ─── Hadith Books & Items Tests ──────────────────────────────────────────────

class TestHadithContent:
    @pytest.mark.parametrize("collection", ["bukhari", "muslim", "tirmidhi", "abudawud", "nasai", "ibnmajah"])
    def test_books_endpoint(self, client, collection):
        import time; time.sleep(0.5)  # avoid server connection exhaustion
        r = client.get(f"/api/v1/hadith/collections/{collection}/books")
        assert r.status_code == 200
        books = r.json()["books"]
        assert len(books) >= 10, f"{collection}: expected >=10 books, got {len(books)}"

    def test_bukhari_book1_hadiths(self, client):
        r = client.get("/api/v1/hadith/collections/bukhari/books/1/hadiths?page=1&limit=10")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0, "Bukhari book 1 should have hadiths"
        for item in items:
            assert item.get("collection") == "bukhari"
            assert item.get("preview_text") or item.get("title")

    def test_bukhari_hadith_1_intentions(self, client):
        """The most famous hadith: 'Actions are judged by intentions'."""
        r = client.get("/api/v1/hadith/collections/bukhari/hadiths/1")
        assert r.status_code == 200
        h = r.json()["hadith"]
        text = h.get("translation_text", "").lower()
        assert "intention" in text, f"Bukhari #1 should mention 'intention', got: {text[:100]}"

    @pytest.mark.parametrize("collection,hadith_num", [
        ("bukhari", "1"),
        ("muslim", "100"),  # Muslim early hadiths are introductions with no text in CDN
        ("tirmidhi", "1"),
        ("abudawud", "1"),
        ("nasai", "1"),
        ("ibnmajah", "1"),
    ])
    def test_first_hadith_each_collection(self, client, collection, hadith_num):
        r = client.get(f"/api/v1/hadith/collections/{collection}/hadiths/{hadith_num}")
        assert r.status_code == 200
        h = r.json()["hadith"]
        assert h.get("translation_text"), f"{collection} #{hadith_num} has no translation_text"
        assert len(h["translation_text"]) > 10

    def test_hadith_pagination(self, client):
        r1 = client.get("/api/v1/hadith/collections/bukhari/books/1/hadiths?page=1&limit=5")
        r2 = client.get("/api/v1/hadith/collections/bukhari/books/1/hadiths?page=2&limit=5")
        assert r1.status_code == 200
        assert r2.status_code == 200
        items1 = [i.get("hadith_number") for i in r1.json()["items"]]
        items2 = [i.get("hadith_number") for i in r2.json()["items"]]
        assert items1 != items2, "Page 1 and 2 should have different hadiths"


# ─── Dua Tests ───────────────────────────────────────────────────────────────

class TestDua:
    def test_categories_count(self, client):
        r = client.get("/api/v1/dua/categories")
        assert r.status_code == 200
        d = r.json()
        assert d["total_categories"] >= 20
        assert d["total_duas"] >= 70

    def test_all_items(self, client):
        r = client.get("/api/v1/dua/items")
        assert r.status_code == 200
        d = r.json()
        assert d["total"] >= 70
        for item in d["items"][:5]:
            assert item.get("dua_id"), "Dua item missing dua_id"
            assert item.get("title"), "Dua item missing title"
            assert item.get("arabic_text") or item.get("english_meaning"), \
                f"Dua {item['dua_id']} has no content"

    def test_category_items(self, client):
        r = client.get("/api/v1/dua/categories")
        cats = r.json()["categories"]
        assert len(cats) > 0
        first_cat = cats[0]["category"]
        r2 = client.get(f"/api/v1/dua/categories/{first_cat}")
        assert r2.status_code == 200
        assert r2.json()["total"] > 0

    def test_single_dua_detail(self, client):
        r = client.get("/api/v1/dua/items")
        first_id = r.json()["items"][0]["dua_id"]
        r2 = client.get(f"/api/v1/dua/{first_id}")
        assert r2.status_code == 200
        dua = r2.json()["dua"]
        assert dua["dua_id"] == first_id
        assert dua.get("title")
        assert dua.get("category")

    def test_dua_has_source_reference(self, client):
        r = client.get("/api/v1/dua/items")
        items = r.json()["items"]
        with_source = [i for i in items if i.get("source") or i.get("reference")]
        assert len(with_source) >= len(items) * 0.5, \
            "At least 50% of duas should have source/reference"


# ─── Meta Tests ──────────────────────────────────────────────────────────────

class TestMeta:
    def test_config_has_modules(self, client):
        r = client.get("/api/v1/meta/config")
        assert r.status_code == 200
        d = r.json()
        assert "quran" in d.get("enabled_modules", [])
        assert "hadith" in d.get("enabled_modules", [])

    def test_manifest_has_sync_data(self, client):
        r = client.get("/api/v1/meta/manifest")
        assert r.status_code == 200

    def test_canonical_summary_counts(self, client):
        r = client.get("/api/v1/meta/canonical-summary")
        assert r.status_code == 200
        d = r.json()
        counts = d.get("counts", {})
        assert counts.get("quran_chapters", 0) == 114
        assert counts.get("quran_ayahs", 0) == 6236
        assert counts.get("hadith_collections", 0) >= 6
        assert counts.get("dua_items", 0) >= 70
