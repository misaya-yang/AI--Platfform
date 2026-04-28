-- =============================================================================
-- 008 — Strip publisher-supplied bidi typesetting marks from Arabic text.
-- =============================================================================
-- Both data sources (sunnah.com via the AhmedElTabarani scraper, AND the
-- fawazahmed0 CDN) embed U+200F RIGHT-TO-LEFT MARK (and very rarely
-- U+200E LEFT-TO-RIGHT MARK) directly into the Arabic body text — usually
-- one immediately before each "." or other punctuation. They were
-- typesetting hints in the original printed editions; modern Unicode bidi
-- algorithms don't need them, but they:
--
--   * render as visible ``[U+200F]`` blocks in JSON inspectors (Apifox /
--     Postman) — perceived as data corruption by Java backend devs;
--   * add ~300 KB of zero-width noise across the DB (~33 778 affected rows);
--   * confuse downstream AI models (the marks are tokenized as random
--     glyphs, breaking semantic search relevance);
--
-- We strip them everywhere they live in user-facing fields. ZWJ (U+200D)
-- and ZWNJ (U+200C) are LEFT ALONE — those are legitimate Arabic joiner
-- controls that change letter shaping (e.g. Persian/Urdu connections).
--
-- Going forward, ``HadithRepository._normalize_arabic`` strips them at
-- both insert and read time, so re-syncs don't reintroduce the noise.
-- =============================================================================

SET search_path = islamic_content, public;

-- 1. Hadith body text (the main offender)
UPDATE hadith_localizations
   SET body_text = replace(replace(body_text, chr(8207), ''), chr(8206), '')
 WHERE language = 'ar'
   AND (position(chr(8207) IN body_text) > 0
        OR position(chr(8206) IN body_text) > 0);

-- 2. Hadith chapter titles (Arabic)
UPDATE hadith_chapters
   SET title_ar = replace(replace(title_ar, chr(8207), ''), chr(8206), '')
 WHERE title_ar IS NOT NULL
   AND (position(chr(8207) IN title_ar) > 0
        OR position(chr(8206) IN title_ar) > 0);

UPDATE hadith_chapters
   SET intro_ar = replace(replace(intro_ar, chr(8207), ''), chr(8206), '')
 WHERE intro_ar IS NOT NULL
   AND (position(chr(8207) IN intro_ar) > 0
        OR position(chr(8206) IN intro_ar) > 0);

-- 3. Mirror copy on hadith_localizations.chapter_title (Arabic-language rows)
UPDATE hadith_localizations
   SET chapter_title = replace(replace(chapter_title, chr(8207), ''), chr(8206), '')
 WHERE language = 'ar'
   AND chapter_title IS NOT NULL
   AND (position(chr(8207) IN chapter_title) > 0
        OR position(chr(8206) IN chapter_title) > 0);

-- Verify expected end-state
DO $verify$
DECLARE
    remaining_rlm BIGINT;
BEGIN
    SELECT COUNT(*) INTO remaining_rlm
      FROM hadith_localizations
     WHERE language = 'ar' AND position(chr(8207) IN body_text) > 0;
    IF remaining_rlm > 0 THEN
        RAISE EXCEPTION 'bidi-strip incomplete: % rows still contain U+200F', remaining_rlm;
    END IF;
END
$verify$;
