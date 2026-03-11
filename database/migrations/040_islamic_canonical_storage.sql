-- Migration: 040_islamic_canonical_storage.sql
-- Purpose: Canonical PostgreSQL storage for Quran / Hadith / Dua / sync audit

CREATE TABLE IF NOT EXISTS source_sync_runs (
    run_id VARCHAR(64) PRIMARY KEY,
    source_name VARCHAR(64) NOT NULL,
    scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_source_sync_runs_source_name
    ON source_sync_runs(source_name, started_at DESC);

CREATE TABLE IF NOT EXISTS source_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    source_name VARCHAR(64) NOT NULL,
    snapshot_kind VARCHAR(64) NOT NULL,
    snapshot_key VARCHAR(255) NOT NULL,
    request_path TEXT,
    request_params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_name, snapshot_kind, snapshot_key)
);

CREATE INDEX IF NOT EXISTS idx_source_snapshots_lookup
    ON source_snapshots(source_name, snapshot_kind, fetched_at DESC);

CREATE TABLE IF NOT EXISTS quran_chapters (
    chapter_id INTEGER PRIMARY KEY,
    name_simple VARCHAR(255),
    name_complex VARCHAR(255),
    name_arabic VARCHAR(255),
    translated_name VARCHAR(255),
    revelation_place VARCHAR(32),
    verses_count INTEGER,
    source_api VARCHAR(64) NOT NULL DEFAULT 'quran.foundation',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quran_translations (
    translation_id INTEGER PRIMARY KEY,
    name VARCHAR(255),
    author_name VARCHAR(255),
    slug VARCHAR(255),
    language_name VARCHAR(64),
    source_api VARCHAR(64) NOT NULL DEFAULT 'quran.foundation',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quran_recitations (
    recitation_id INTEGER PRIMARY KEY,
    reciter_name VARCHAR(255),
    style VARCHAR(255),
    translated_name VARCHAR(255),
    source_api VARCHAR(64) NOT NULL DEFAULT 'quran.foundation',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quran_ayahs (
    verse_key VARCHAR(16) PRIMARY KEY,
    chapter_id INTEGER NOT NULL REFERENCES quran_chapters(chapter_id) ON DELETE CASCADE,
    ayah_number INTEGER NOT NULL,
    juz_number INTEGER,
    hizb_number INTEGER,
    rub_number INTEGER,
    page_number INTEGER,
    arabic_text TEXT NOT NULL DEFAULT '',
    transliteration_text TEXT NOT NULL DEFAULT '',
    default_translation_id INTEGER,
    translation_text TEXT NOT NULL DEFAULT '',
    default_recitation_id INTEGER,
    audio_url TEXT,
    source_api VARCHAR(64) NOT NULL DEFAULT 'quran.foundation',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chapter_id, ayah_number)
);

CREATE INDEX IF NOT EXISTS idx_quran_ayahs_chapter_ayah
    ON quran_ayahs(chapter_id, ayah_number);

CREATE TABLE IF NOT EXISTS quran_words (
    id BIGSERIAL PRIMARY KEY,
    verse_key VARCHAR(16) NOT NULL REFERENCES quran_ayahs(verse_key) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    arabic TEXT,
    arabic_simple TEXT,
    transliteration TEXT,
    translation TEXT,
    audio_url TEXT,
    char_type VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (verse_key, position)
);

CREATE INDEX IF NOT EXISTS idx_quran_words_verse_position
    ON quran_words(verse_key, position);

CREATE TABLE IF NOT EXISTS quran_triplet_blocks (
    block_id VARCHAR(128) PRIMARY KEY,
    chapter_id INTEGER NOT NULL REFERENCES quran_chapters(chapter_id) ON DELETE CASCADE,
    start_ayah_number INTEGER NOT NULL,
    end_ayah_number INTEGER NOT NULL,
    translation_id INTEGER NOT NULL,
    recitation_id INTEGER NOT NULL,
    verse_keys_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    arabic_text TEXT NOT NULL DEFAULT '',
    transliteration_text TEXT NOT NULL DEFAULT '',
    translation_text TEXT NOT NULL DEFAULT '',
    audio_urls_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chapter_id, start_ayah_number, end_ayah_number, translation_id, recitation_id)
);

CREATE INDEX IF NOT EXISTS idx_quran_triplet_blocks_chapter_range
    ON quran_triplet_blocks(chapter_id, start_ayah_number, end_ayah_number);

CREATE TABLE IF NOT EXISTS hadith_collections (
    name VARCHAR(64) PRIMARY KEY,
    title VARCHAR(255),
    short_intro TEXT,
    has_books BOOLEAN,
    has_chapters BOOLEAN,
    total_books INTEGER,
    total_hadith INTEGER,
    source_api VARCHAR(64) NOT NULL DEFAULT 'sunnah',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hadith_books (
    id BIGSERIAL PRIMARY KEY,
    collection_name VARCHAR(64) NOT NULL REFERENCES hadith_collections(name) ON DELETE CASCADE,
    book_number VARCHAR(32) NOT NULL,
    title VARCHAR(255),
    hadith_start_number INTEGER,
    hadith_end_number INTEGER,
    number_of_hadith INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (collection_name, book_number)
);

CREATE INDEX IF NOT EXISTS idx_hadith_books_collection_book
    ON hadith_books(collection_name, book_number);

CREATE TABLE IF NOT EXISTS hadith_items (
    id BIGSERIAL PRIMARY KEY,
    collection_name VARCHAR(64) NOT NULL REFERENCES hadith_collections(name) ON DELETE CASCADE,
    book_number VARCHAR(32) NOT NULL,
    chapter_id VARCHAR(64),
    hadith_number VARCHAR(64) NOT NULL,
    chapter_title VARCHAR(255),
    source_api VARCHAR(64) NOT NULL DEFAULT 'sunnah',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (collection_name, hadith_number)
);

CREATE INDEX IF NOT EXISTS idx_hadith_items_collection_book
    ON hadith_items(collection_name, book_number, hadith_number);

CREATE TABLE IF NOT EXISTS hadith_localizations (
    id BIGSERIAL PRIMARY KEY,
    hadith_item_id BIGINT NOT NULL REFERENCES hadith_items(id) ON DELETE CASCADE,
    language VARCHAR(8) NOT NULL,
    chapter_title VARCHAR(255),
    body_text TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (hadith_item_id, language)
);

CREATE TABLE IF NOT EXISTS hadith_grades (
    id BIGSERIAL PRIMARY KEY,
    hadith_item_id BIGINT NOT NULL REFERENCES hadith_items(id) ON DELETE CASCADE,
    language VARCHAR(8) NOT NULL,
    grade VARCHAR(255),
    graded_by VARCHAR(255),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hadith_grades_item_language
    ON hadith_grades(hadith_item_id, language);

CREATE TABLE IF NOT EXISTS dua_categories (
    category VARCHAR(255) PRIMARY KEY,
    dua_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dua_items (
    dua_id VARCHAR(128) PRIMARY KEY,
    category VARCHAR(255) NOT NULL REFERENCES dua_categories(category) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    arabic_text TEXT NOT NULL DEFAULT '',
    transliteration_text TEXT NOT NULL DEFAULT '',
    translation_text TEXT NOT NULL DEFAULT '',
    reference TEXT NOT NULL DEFAULT '',
    benefit TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dua_items_category
    ON dua_items(category);
