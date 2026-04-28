-- =============================================================================
-- 012 — Final encoding cleanup: U+FFFD replacement char + advanced bidi
-- controls beyond RLM/LRM that 008/009 didn't catch.
-- =============================================================================
-- Cross-source audit (full_audit.sql) flagged two new categories of noise
-- in the Hadith data:
--
--   * 59 hadith bodies (ar-only, across abudawud/bukhari/ibnmajah/nasai/
--     tirmidhi) contain U+FFFD REPLACEMENT CHARACTER. Verified upstream
--     fawazahmed0 source itself contains U+FFFD ('��' visible in the text)
--     — the original ASCII→UTF-8 conversion at fawazahmed0 lost glyphs.
--     Not our bug, but we shouldn't propagate corrupted output. Strip
--     them; the affected sentences will read with the gap silent rather
--     than a question-mark glyph.
--
--   * 1 hadith body (nawawi/2 ar) contains U+202B RIGHT-TO-LEFT EMBEDDING.
--     This is an old-style bidi control superseded by U+2067 RLI. 008/009
--     only stripped RLM/LRM. Strip the full U+202A-U+202E and U+2066-U+2069
--     ranges so the surface is genuinely free of bidi controls.
--
-- ZWJ (U+200D) and ZWNJ (U+200C) remain preserved — those are legitimate
-- Arabic/Persian letter-shaping joiners.
-- =============================================================================

SET search_path = islamic_content, public;

-- Helper: strip a list of single-char bidi-control codepoints from a text.
-- Inline this as a series of replace() calls — keeps the migration simple
-- and avoids creating a function that survives.

-- ----- 1. Strip U+FFFD replacement char from all text fields ---------------
UPDATE hadith_localizations
   SET body_text = replace(body_text, chr(65533), '')
 WHERE position(chr(65533) IN body_text) > 0;

UPDATE hadith_chapters
   SET title_en = replace(title_en, chr(65533), '')
 WHERE title_en IS NOT NULL AND position(chr(65533) IN title_en) > 0;

UPDATE hadith_chapters
   SET title_ar = replace(title_ar, chr(65533), '')
 WHERE title_ar IS NOT NULL AND position(chr(65533) IN title_ar) > 0;

UPDATE hadith_chapters
   SET intro_en = replace(intro_en, chr(65533), '')
 WHERE intro_en IS NOT NULL AND position(chr(65533) IN intro_en) > 0;

UPDATE hadith_chapters
   SET intro_ar = replace(intro_ar, chr(65533), '')
 WHERE intro_ar IS NOT NULL AND position(chr(65533) IN intro_ar) > 0;

UPDATE quran_ayahs
   SET arabic_text = replace(arabic_text, chr(65533), '')
 WHERE position(chr(65533) IN arabic_text) > 0;

UPDATE quran_ayah_translations
   SET translation_text = replace(translation_text, chr(65533), '')
 WHERE position(chr(65533) IN translation_text) > 0;

UPDATE dua_items
   SET arabic_text = replace(arabic_text, chr(65533), ''),
       english_meaning = replace(english_meaning, chr(65533), ''),
       transliteration = replace(transliteration, chr(65533), ''),
       urdu_meaning = replace(urdu_meaning, chr(65533), '')
 WHERE position(chr(65533) IN arabic_text) > 0
    OR position(chr(65533) IN english_meaning) > 0
    OR position(chr(65533) IN transliteration) > 0
    OR position(chr(65533) IN urdu_meaning) > 0;

-- ----- 2. Strip advanced bidi controls (U+202A..U+202E, U+2066..U+2069) ----
-- Apply via repeated replace() — one call per codepoint. 9 codepoints total.
DO $strip_bidi$
DECLARE
    cp INT;
    cps INT[] := ARRAY[8234, 8235, 8236, 8237, 8238,  -- U+202A..U+202E
                       8294, 8295, 8296, 8297];        -- U+2066..U+2069
BEGIN
    FOREACH cp IN ARRAY cps LOOP
        EXECUTE format(
            'UPDATE hadith_localizations SET body_text = replace(body_text, chr(%s), '''') WHERE position(chr(%s) IN body_text) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE hadith_chapters SET title_en = replace(title_en, chr(%s), '''') WHERE title_en IS NOT NULL AND position(chr(%s) IN title_en) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE hadith_chapters SET title_ar = replace(title_ar, chr(%s), '''') WHERE title_ar IS NOT NULL AND position(chr(%s) IN title_ar) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE hadith_chapters SET intro_en = replace(intro_en, chr(%s), '''') WHERE intro_en IS NOT NULL AND position(chr(%s) IN intro_en) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE hadith_chapters SET intro_ar = replace(intro_ar, chr(%s), '''') WHERE intro_ar IS NOT NULL AND position(chr(%s) IN intro_ar) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE quran_ayahs SET arabic_text = replace(arabic_text, chr(%s), '''') WHERE position(chr(%s) IN arabic_text) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE quran_ayah_translations SET translation_text = replace(translation_text, chr(%s), '''') WHERE position(chr(%s) IN translation_text) > 0',
            cp, cp);
        EXECUTE format(
            'UPDATE dua_items SET arabic_text = replace(arabic_text, chr(%s), '''') WHERE position(chr(%s) IN arabic_text) > 0',
            cp, cp);
    END LOOP;
END
$strip_bidi$;

-- ----- 3. Verify ----------------------------------------------------------
DO $verify$
DECLARE
    repl_count BIGINT;
    bidi_count BIGINT;
BEGIN
    -- replacement chars
    SELECT COUNT(*) INTO repl_count FROM hadith_localizations
     WHERE position(chr(65533) IN body_text) > 0;
    IF repl_count > 0 THEN
        RAISE EXCEPTION 'U+FFFD strip incomplete: % rows still have replacement char', repl_count;
    END IF;

    -- advanced bidi
    SELECT COUNT(*) INTO bidi_count FROM hadith_localizations hl
     WHERE EXISTS (
         SELECT 1 FROM unnest(ARRAY[8234,8235,8236,8237,8238,8294,8295,8296,8297]) cp
          WHERE position(chr(cp) IN hl.body_text) > 0
       );
    IF bidi_count > 0 THEN
        RAISE EXCEPTION 'advanced bidi strip incomplete: % rows still have bidi controls', bidi_count;
    END IF;
END
$verify$;
