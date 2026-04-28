-- =============================================================================
-- 010 — Drop 167 phantom hadiths (no body text) + recount books / chapters
-- / collection totals.
-- =============================================================================
-- Audit pass after 009 found two remaining drifts:
--
--   * 167 hadith_items rows have NO localization in either language. Sample:
--     bukhari hadith_number 5710 / 5711 / 5712 / 5774 / 6074 / 6174 ...
--     Confirmed against fawazahmed0 source (text="" empty string), so
--     these are upstream-empty placeholder entries — the CDN serves a
--     hadithnumber + reference but no body. Useless to end users (click
--     opens an empty page). Remove cleanly so the API surface contains
--     only viewable hadiths.
--
--   * 114 hadith_books.number_of_hadith rows drift vs actual COUNT(*) in
--     hadith_items, totalling 10 561 over-counted (muslim alone = 8 928,
--     a leftover from the fawazahmed0 → sunnah.com rebuild that dropped
--     7 563 → 7 205 modern-numbered hadiths). Recompute from actual count.
--
-- Same for hadith_collections.total_hadith / total_books and
-- hadith_chapters.hadith_count (defensive — 009 already aligned chapters).
-- =============================================================================

SET search_path = islamic_content, public;

-- ----- 1. Drop grades attached to the phantom hadiths ----------------------
DELETE FROM hadith_grades
 WHERE hadith_item_id IN (
       SELECT hi.id FROM hadith_items hi
        WHERE NOT EXISTS (
              SELECT 1 FROM hadith_localizations hl
               WHERE hl.hadith_item_id = hi.id
          )
   );

-- ----- 2. Drop the phantom hadiths themselves ------------------------------
DELETE FROM hadith_items hi
 WHERE NOT EXISTS (
       SELECT 1 FROM hadith_localizations hl
        WHERE hl.hadith_item_id = hi.id
   );

-- ----- 3. Recompute hadith_books.number_of_hadith --------------------------
UPDATE hadith_books hb
   SET number_of_hadith = COALESCE(s.cnt, 0)
  FROM (
        SELECT collection_name, book_number, COUNT(*) AS cnt
          FROM hadith_items
         GROUP BY collection_name, book_number
       ) s
 WHERE s.collection_name = hb.collection_name
   AND s.book_number     = hb.book_number;

-- Books that exist in hadith_books but have 0 hadiths (e.g. nawawi book 1
-- if its hadiths got cleaned, or an empty placeholder) → set to 0.
UPDATE hadith_books hb
   SET number_of_hadith = 0
 WHERE NOT EXISTS (
       SELECT 1 FROM hadith_items hi
        WHERE hi.collection_name = hb.collection_name
          AND hi.book_number     = hb.book_number
   )
   AND hb.number_of_hadith <> 0;

-- ----- 4. Recompute hadith_collections.total_hadith / total_books ----------
UPDATE hadith_collections hcol
   SET total_hadith = COALESCE((
       SELECT COUNT(*) FROM hadith_items hi
        WHERE hi.collection_name = hcol.name
   ), 0),
       total_books = COALESCE((
       SELECT COUNT(DISTINCT book_number) FROM hadith_items hi
        WHERE hi.collection_name = hcol.name
   ), 0);

-- ----- 5. Recompute hadith_chapters.hadith_count (defensive) ---------------
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

UPDATE hadith_chapters hc
   SET hadith_count = 0
 WHERE NOT EXISTS (
       SELECT 1 FROM hadith_items hi WHERE hi.chapter_ref_id = hc.id
   )
   AND hc.hadith_count <> 0;

-- ----- Verify end-state ----------------------------------------------------
DO $verify$
DECLARE
    phantoms      BIGINT;
    book_drifts   BIGINT;
    chap_drifts   BIGINT;
    coll_drifts   BIGINT;
BEGIN
    SELECT COUNT(*) INTO phantoms
      FROM hadith_items hi
     WHERE NOT EXISTS (SELECT 1 FROM hadith_localizations hl WHERE hl.hadith_item_id = hi.id);
    IF phantoms > 0 THEN
        RAISE EXCEPTION 'phantom cleanup incomplete: % rows still have no localization', phantoms;
    END IF;

    SELECT COUNT(*) INTO book_drifts
      FROM hadith_books hb
     WHERE hb.number_of_hadith IS DISTINCT FROM (
           SELECT COUNT(*) FROM hadith_items hi
            WHERE hi.collection_name = hb.collection_name
              AND hi.book_number     = hb.book_number
       );
    IF book_drifts > 0 THEN
        RAISE EXCEPTION 'book count drift survives: % rows', book_drifts;
    END IF;

    SELECT COUNT(*) INTO chap_drifts
      FROM hadith_chapters hc
     WHERE hc.hadith_count IS DISTINCT FROM (
           SELECT COUNT(*) FROM hadith_items hi WHERE hi.chapter_ref_id = hc.id
       );
    IF chap_drifts > 0 THEN
        RAISE EXCEPTION 'chapter count drift survives: % rows', chap_drifts;
    END IF;

    SELECT COUNT(*) INTO coll_drifts
      FROM hadith_collections hcol
     WHERE hcol.total_hadith IS DISTINCT FROM (
           SELECT COUNT(*) FROM hadith_items hi WHERE hi.collection_name = hcol.name
       );
    IF coll_drifts > 0 THEN
        RAISE EXCEPTION 'collection total drift survives: % rows', coll_drifts;
    END IF;
END
$verify$;
