SET search_path TO __ISLAMIC_CONTENT_SCHEMA__, public;

CREATE TABLE IF NOT EXISTS dua_categories (
    category VARCHAR(128) PRIMARY KEY,
    dua_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dua_items (
    dua_id VARCHAR(32) PRIMARY KEY,
    category VARCHAR(128) NOT NULL REFERENCES dua_categories(category),
    title VARCHAR(512) NOT NULL,
    arabic_text TEXT NOT NULL DEFAULT '',
    transliteration TEXT NOT NULL DEFAULT '',
    english_meaning TEXT NOT NULL DEFAULT '',
    urdu_meaning TEXT NOT NULL DEFAULT '',
    source VARCHAR(256) NOT NULL DEFAULT '',
    reference VARCHAR(256) NOT NULL DEFAULT '',
    authenticity VARCHAR(64) NOT NULL DEFAULT '',
    occasion TEXT NOT NULL DEFAULT '',
    data_source VARCHAR(256) NOT NULL DEFAULT '',
    verification_status VARCHAR(128) NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dua_items_category ON dua_items(category);
