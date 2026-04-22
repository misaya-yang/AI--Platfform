SET search_path TO __ISLAMIC_CONTENT_SCHEMA__, public;

-- Real hadith chapter hierarchy, sourced from sunnah.com via
-- AhmedElTabarani/sunnah-hadith-api. Before this table, the only
-- "chapter" data we had was a duplicate of book_number, which is why
-- every book appeared to have exactly one chapter.
CREATE TABLE IF NOT EXISTS hadith_chapters (
    id BIGSERIAL PRIMARY KEY,
    collection_name VARCHAR(64) NOT NULL REFERENCES hadith_collections(name) ON DELETE CASCADE,
    book_number VARCHAR(32) NOT NULL,
    chapter_order INTEGER NOT NULL,
    chapter_id_raw VARCHAR(32),
    title_en TEXT,
    title_ar TEXT,
    intro_en TEXT,
    intro_ar TEXT,
    hadith_count INTEGER,
    source_api VARCHAR(64) NOT NULL DEFAULT 'sunnah.com',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (collection_name, book_number, chapter_order)
);

CREATE INDEX IF NOT EXISTS idx_hadith_chapters_book
    ON hadith_chapters(collection_name, book_number, chapter_order);

ALTER TABLE hadith_items
    ADD COLUMN IF NOT EXISTS chapter_ref_id BIGINT
    REFERENCES hadith_chapters(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_hadith_items_chapter_ref_id
    ON hadith_items(chapter_ref_id);
