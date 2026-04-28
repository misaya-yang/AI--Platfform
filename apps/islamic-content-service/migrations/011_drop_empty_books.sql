-- =============================================================================
-- 011 — Drop the 2 empty hadith_books rows after phantom cleanup.
-- =============================================================================
-- After 010 deleted the 167 phantom hadiths, two book rows became
-- empty:
--   muslim / book_number=0  (Introduction — was 100% phantoms upstream)
--   nasai  / book_number=0  (Introduction — same story)
--
-- These show up in /collections/{c}/books because hadith_books still has
-- their row, but they have NO hadiths to serve, NO chapters worth
-- listing, and number_of_hadith=0. Causes:
--
--   * /collections/muslim/books returns 57 books while
--     collection.total_books (computed from DISTINCT book_number on
--     hadith_items) returns 56 → API self-inconsistency Java flagged.
--   * GET /muslim/books/0/chapters → 503 "No chapters found"
--     (NotReadyError on empty result is mapped to 503 in the route).
--
-- Drop both books + the 1 dangling synthetic "Introduction (Unmapped
-- preamble hadiths)" chapter that had no hadiths after the cleanup.
-- Bukhari book 0 stays — it has 306 real hadiths (gotcha #5 in
-- reference_islamic_content_service.md is still load-bearing for it).
-- =============================================================================

SET search_path = islamic_content, public;

-- Drop the dangling synthetic chapter (only nasai/0 had one; muslim/0
-- never had a synthetic chapter to begin with — its phantoms didn't get
-- one assigned).
DELETE FROM hadith_chapters
 WHERE (collection_name = 'muslim' AND book_number = '0')
    OR (collection_name = 'nasai'  AND book_number = '0');

-- Drop the 2 empty book rows. SAFETY: only delete when 0 hadith_items
-- reference this (collection, book) — never delete a book that still
-- has content.
DELETE FROM hadith_books hb
 WHERE hb.number_of_hadith = 0
   AND NOT EXISTS (
         SELECT 1 FROM hadith_items hi
          WHERE hi.collection_name = hb.collection_name
            AND hi.book_number     = hb.book_number
       );

-- Verify
DO $verify$
DECLARE
    empty_books BIGINT;
BEGIN
    SELECT COUNT(*) INTO empty_books
      FROM hadith_books hb
     WHERE NOT EXISTS (
           SELECT 1 FROM hadith_items hi
            WHERE hi.collection_name = hb.collection_name
              AND hi.book_number     = hb.book_number
       );
    IF empty_books > 0 THEN
        RAISE EXCEPTION '011 incomplete: % empty book(s) survive', empty_books;
    END IF;
END
$verify$;
