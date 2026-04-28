-- =============================================================================
-- 009 — Repair cross-book chapter_ref_id linkage + finish bidi-mark cleanup.
-- =============================================================================
-- Problem A — 264 hadiths point at chapters in a DIFFERENT book:
--   bukhari : 256 (almost all of book=0 "Introduction" was misrouted)
--   ibnmajah:   4
--   tirmidhi:   4
-- These slipped through ``backfill_orphan_hadith_chapters.py`` because the
-- scraper-time linkage matched hadith_number ranges that overlap across
-- books in fawazahmed0's flat numbering. Result: ``GET .../bukhari/books``
-- says ``number_of_hadith=311`` for book=0 but ``GET .../books/0/chapters``
-- only sums to 55 — Java side correctly flagged the mismatch.
--
-- Fix: for every (collection, book) with misrouted rows, find (or create)
-- the per-book catch-all chapter, then re-point ``chapter_ref_id`` so a
-- hadith in book=N always links to a chapter in book=N. After re-linking
-- we recompute ``hadith_count`` on every chapter (mirrors what
-- ``hadith_chapter_maintenance.py`` does).
--
-- Problem B — Bidi mark scrub on the EN side
--   008 only stripped Arabic fields. 1 chapter (bukhari book 2 intro_en)
--   carries a stray U+200E that Apifox/Postman highlight — same UX bug as
--   the Arabic case. Strip RLM + LRM from intro_en / title_en /
--   localizations.body_text(en) too.
-- =============================================================================

SET search_path = islamic_content, public;

-- ----- Problem A : repair cross-book chapter_ref_id ------------------------
DO $fix_linkage$
DECLARE
    rec RECORD;
    target_id BIGINT;
BEGIN
    FOR rec IN
        SELECT DISTINCT hi.collection_name, hi.book_number
          FROM hadith_items hi
          JOIN hadith_chapters hc ON hc.id = hi.chapter_ref_id
         WHERE hi.book_number <> hc.book_number
    LOOP
        -- Find existing catch-all chapter in the hadith's OWN book.
        -- Catch-alls are titled "Introduction (Unmapped preamble hadiths)"
        -- or "Additional hadiths (not grouped by sunnah.com)" per the
        -- backfill script convention.
        SELECT id INTO target_id
          FROM hadith_chapters
         WHERE collection_name = rec.collection_name
           AND book_number      = rec.book_number
           AND (title_en ILIKE '%Unmapped%' OR title_en ILIKE '%not grouped%')
         ORDER BY chapter_order DESC
         LIMIT 1;

        -- No catch-all yet → mint one with chapter_order=999 so it sorts
        -- last in any chapter listing.
        IF target_id IS NULL THEN
            INSERT INTO hadith_chapters
                (collection_name, book_number, chapter_order, chapter_id_raw,
                 title_en, title_ar, intro_en, intro_ar, hadith_count)
            VALUES
                (rec.collection_name, rec.book_number, 999, NULL,
                 'Additional hadiths (not grouped by sunnah.com)',
                 NULL, NULL, NULL, 0)
            RETURNING id INTO target_id;
        END IF;

        -- Re-link every misrouted hadith in this (collection, book) to
        -- the catch-all of its OWN book.
        UPDATE hadith_items hi
           SET chapter_ref_id = target_id
         WHERE hi.collection_name = rec.collection_name
           AND hi.book_number      = rec.book_number
           AND hi.chapter_ref_id IN (
                 SELECT id FROM hadith_chapters
                  WHERE collection_name = rec.collection_name
                    AND book_number     <> rec.book_number
               );
    END LOOP;
END
$fix_linkage$;

-- ----- Recompute hadith_count on every chapter ------------------------------
-- This is a single UPDATE … FROM (SELECT … GROUP BY …) so it touches every
-- chapter once. Mirrors hadith_chapter_maintenance.py's drift-repair step.
UPDATE hadith_chapters hc
   SET hadith_count = COALESCE(s.cnt, 0)
  FROM (
        SELECT chapter_ref_id, COUNT(*) AS cnt
          FROM hadith_items
         WHERE chapter_ref_id IS NOT NULL
         GROUP BY chapter_ref_id
       ) s
 WHERE s.chapter_ref_id = hc.id
   AND hc.hadith_count IS DISTINCT FROM COALESCE(s.cnt, 0);

-- Chapters that no hadith points at any longer → hadith_count = 0
UPDATE hadith_chapters hc
   SET hadith_count = 0
 WHERE NOT EXISTS (
         SELECT 1 FROM hadith_items hi WHERE hi.chapter_ref_id = hc.id
       )
   AND hc.hadith_count <> 0;

-- ----- Problem B : LRM/RLM scrub on EN fields ------------------------------
UPDATE hadith_chapters
   SET intro_en = replace(replace(intro_en, chr(8207), ''), chr(8206), '')
 WHERE intro_en IS NOT NULL
   AND (position(chr(8207) IN intro_en) > 0
        OR position(chr(8206) IN intro_en) > 0);

UPDATE hadith_chapters
   SET title_en = replace(replace(title_en, chr(8207), ''), chr(8206), '')
 WHERE title_en IS NOT NULL
   AND (position(chr(8207) IN title_en) > 0
        OR position(chr(8206) IN title_en) > 0);

UPDATE hadith_localizations
   SET body_text = replace(replace(body_text, chr(8207), ''), chr(8206), '')
 WHERE language = 'en'
   AND (position(chr(8207) IN body_text) > 0
        OR position(chr(8206) IN body_text) > 0);

-- ----- Verify end-state ----------------------------------------------------
DO $verify$
DECLARE
    cross_book_count BIGINT;
    en_lrm_count     BIGINT;
BEGIN
    SELECT COUNT(*) INTO cross_book_count
      FROM hadith_items hi
      JOIN hadith_chapters hc ON hc.id = hi.chapter_ref_id
     WHERE hi.book_number <> hc.book_number;
    IF cross_book_count > 0 THEN
        RAISE EXCEPTION 'cross-book linkage repair incomplete: % rows still misrouted',
            cross_book_count;
    END IF;

    SELECT COUNT(*) INTO en_lrm_count
      FROM hadith_chapters
     WHERE intro_en IS NOT NULL AND position(chr(8206) IN intro_en) > 0;
    IF en_lrm_count > 0 THEN
        RAISE EXCEPTION 'EN bidi-strip incomplete: % chapter rows still have LRM',
            en_lrm_count;
    END IF;
END
$verify$;
