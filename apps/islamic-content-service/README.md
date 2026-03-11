# Islamic Content Service

Standalone Quran and Hadith content microservice extracted from `ai-gateway`.

## Capabilities

- Bootstrap Quran and Hadith content into PostgreSQL
- Serve low-latency read APIs from PostgreSQL and Redis only
- Expose independent Swagger docs at `/docs`
- Keep a dedicated `islamic_content` schema inside the shared PostgreSQL database

## Quick Start

1. Install the package in editable mode
2. Copy `.env.example` to `.env`
3. Fill Quran Foundation and Sunnah credentials
4. Run migrations and bootstrap

```bash
cd apps/islamic-content-service
pip install -e '.[dev]'
cp .env.example .env
python -m islamic_content_service.cli db migrate
python -m islamic_content_service.cli sync bootstrap --sources quran,hadith
islamic-content-service
```

To bootstrap the full Quran across every synced translation and recitation, leave
`ISLAMIC_CONTENT_QURAN__SYNC_ALL_TRANSLATIONS=true` and
`ISLAMIC_CONTENT_QURAN__SYNC_ALL_RECITATIONS=true`. To enable Quran user OAuth
and User API proxy routes, also fill the `ISLAMIC_CONTENT_QURAN_USER__*`
variables in `.env`.

## Public APIs

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/meta/config`
- `GET /api/v1/meta/manifest`
- `GET /api/v1/meta/canonical-summary`
- `GET /api/v1/quran/...`
- `GET /api/v1/quran/chapters/{chapter_id}/audio-text`
- `GET /api/v1/quran/user/...`
- `GET /api/v1/hadith/...`

`/api/v1/quran/chapters/{chapter_id}/audio-text` returns chapter audio, verse text,
word text, verse-level timings, and word-level segments in a single payload.
Most Quran read routes also accept optional `translation_id` and/or `recitation_id`
query params, so the service can return any synced translation or recitation instead
of only the defaults.

## API Test Script

Use the bundled test script to see each endpoint's required parameters and to call
the APIs directly:

```bash
cd apps/islamic-content-service
python scripts/test_public_api.py --list
python scripts/test_public_api.py --smoke
python scripts/test_public_api.py --endpoint quran_audio_text --chapter-id 1
python scripts/test_public_api.py --endpoint quran_audio_text --chapter-id 1 --translation-id 84 --recitation-id 1
python scripts/test_public_api.py --endpoint quran_ayah_detail --verse-key 1:1
python scripts/test_public_api.py --endpoint quran_ayah_minimal --verse-key 1:1
python scripts/test_public_api.py --endpoint quran_user_authorize_url --redirect-uri https://wahda.example/callback --state abc123
```

If you want JSON files written to disk:

```bash
python scripts/test_public_api.py --smoke --output-dir ./tmp/api-responses
```

The script covers:
- Health: no params
- Meta: no params
- Quran chapter routes: `chapter_id`
- Quran ayah routes: `verse_key`
- Quran variant selection: optional `translation_id`, `recitation_id`
- Quran user OAuth helper: optional `redirect_uri`, `state`, `code_challenge`
- Hadith book list: `collection_name`, `book_number`, optional `page`, `limit`
- Hadith detail: `collection_name`, `hadith_number`

Minimal ayah text-only route:
- `GET /api/v1/quran/ayahs/{verse_key}/minimal`
- Returns only:
  - `arabic_text`
  - `transliteration_text`
  - `translation_text`
- Plus minimal identifiers:
  - `verse_key`
  - `surah_number`
  - `ayah_number`
  - `translation_id`
  - `recitation_id`

## Notes

- Public read APIs never call upstream Quran or Sunnah APIs directly.
- Sync is CLI-only in v1.
- Consumers are expected to reach this service over internal/private networking in v1.
