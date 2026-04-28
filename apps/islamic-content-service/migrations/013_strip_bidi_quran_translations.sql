-- =============================================================================
-- 013 — Strip RLM/LRM from Quran ayah translations.
-- =============================================================================
-- 008 only scrubbed Hadith fields. Cross-source audit found Quran ayah
-- translations also carry bidi marks (765 rows total):
--
--   * Malayalam (Muhammad Karakunnu): 691 LRM rows
--   * Kurdish (Burhan Muhammad-Amin):  42 LRM rows
--   * Bengali (Bayaan Foundation):     20 LRM rows
--   * Persian (IslamHouse.com):         1 LRM + 3 RLM
--   * Other (Asante / English / Persian Taji): 4 rows total
--
-- These are publisher-supplied bidi hints from the Quran Foundation
-- source. Modern Unicode bidi handles direction without them; stripping
-- gives Java consumers clean output that doesn't render as visible
-- ``[U+200E]`` markers in JSON inspectors.
-- =============================================================================

SET search_path = islamic_content, public;

UPDATE quran_ayah_translations
   SET translation_text = replace(replace(translation_text, chr(8207), ''), chr(8206), '')
 WHERE position(chr(8207) IN translation_text) > 0
    OR position(chr(8206) IN translation_text) > 0;

-- Defensive: also Quran arabic_text (audit said 0 but cheap to check).
UPDATE quran_ayahs
   SET arabic_text = replace(replace(arabic_text, chr(8207), ''), chr(8206), '')
 WHERE position(chr(8207) IN arabic_text) > 0
    OR position(chr(8206) IN arabic_text) > 0;

-- Defensive: Quran ayahs translation_text (the inline column on quran_ayahs).
UPDATE quran_ayahs
   SET translation_text = replace(replace(translation_text, chr(8207), ''), chr(8206), '')
 WHERE position(chr(8207) IN translation_text) > 0
    OR position(chr(8206) IN translation_text) > 0;

-- Defensive: Dua all text columns (audit said 0 but cheap).
UPDATE dua_items
   SET arabic_text     = replace(replace(arabic_text, chr(8207), ''), chr(8206), ''),
       english_meaning = replace(replace(english_meaning, chr(8207), ''), chr(8206), ''),
       transliteration = replace(replace(transliteration, chr(8207), ''), chr(8206), ''),
       urdu_meaning    = replace(replace(urdu_meaning, chr(8207), ''), chr(8206), '')
 WHERE position(chr(8207) IN arabic_text) > 0     OR position(chr(8206) IN arabic_text) > 0
    OR position(chr(8207) IN english_meaning) > 0 OR position(chr(8206) IN english_meaning) > 0
    OR position(chr(8207) IN transliteration) > 0 OR position(chr(8206) IN transliteration) > 0
    OR position(chr(8207) IN urdu_meaning) > 0    OR position(chr(8206) IN urdu_meaning) > 0;

DO $verify$
DECLARE
    n_quran BIGINT;
BEGIN
    SELECT COUNT(*) INTO n_quran FROM quran_ayah_translations
     WHERE position(chr(8207) IN translation_text) > 0
        OR position(chr(8206) IN translation_text) > 0;
    IF n_quran > 0 THEN
        RAISE EXCEPTION 'Quran translation bidi strip incomplete: % rows', n_quran;
    END IF;
END
$verify$;
