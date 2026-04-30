-- Cross-source data integrity audit for Quran, Hadith, and Dua.
-- Run with:
--   psql "$DATABASE_URL" -f scripts/audit_full_data.sql
--
-- Healthy production data should report 0 violations for all violation
-- checks, and PASS for fixed-count checks.
--
-- PostgreSQL text values cannot contain a literal NUL byte. The NUL checks
-- below are kept as explicit invariant rows, but they do not call chr(0)
-- because PostgreSQL rejects that expression before it can be evaluated.

\set ON_ERROR_STOP on
SET search_path = islamic_content, public;

\echo
\echo ##############################################################
\echo # SECTION 1 - QURAN
\echo ##############################################################

\echo Q1: 114 chapters total
SELECT 'Q1.chapter_count' AS check, COUNT(*) AS actual, 114 AS expected,
       CASE WHEN COUNT(*) = 114 THEN 'PASS' ELSE 'FAIL' END AS status
FROM quran_chapters;

\echo Q2: every chapter has verses_count > 0
SELECT 'Q2.zero_verses_count' AS check, COUNT(*) AS violations
FROM quran_chapters
WHERE verses_count IS NULL OR verses_count <= 0;

\echo Q3: stored verses_count equals actual ayah count
SELECT 'Q3.verses_count_drift' AS check, COUNT(*) AS violations
FROM quran_chapters c
WHERE c.verses_count IS DISTINCT FROM (
    SELECT COUNT(*) FROM quran_ayahs a WHERE a.chapter_id = c.chapter_id
);

\echo Q4: verse_key format and chapter/ayah consistency
SELECT 'Q4.bad_verse_key' AS check, COUNT(*) AS violations
FROM quran_ayahs
WHERE verse_key !~ '^[0-9]+:[0-9]+$'
   OR verse_key <> chapter_id::text || ':' || ayah_number::text;

\echo Q5: ayah_number sequential 1..N per chapter
SELECT 'Q5.ayah_seq_gap' AS check, COUNT(*) AS violations
FROM (
    SELECT chapter_id, ayah_number,
           ayah_number - LAG(ayah_number) OVER (
               PARTITION BY chapter_id ORDER BY ayah_number
           ) AS gap
    FROM quran_ayahs
) t
WHERE gap IS NOT NULL AND gap <> 1;

\echo Q6: total ayahs = 6236
SELECT 'Q6.total_ayahs' AS check, COUNT(*) AS actual, 6236 AS expected,
       CASE WHEN COUNT(*) = 6236 THEN 'PASS' ELSE 'FAIL' END AS status
FROM quran_ayahs;

\echo Q7: every ayah has non-empty arabic_text
SELECT 'Q7.empty_arabic' AS check, COUNT(*) AS violations
FROM quran_ayahs
WHERE arabic_text IS NULL OR length(trim(arabic_text)) = 0;

\echo Q8: RLM/LRM in Quran arabic_text
SELECT 'Q8.quran_arabic_bidi' AS check,
       SUM(CASE WHEN position(chr(8207) IN arabic_text) > 0 THEN 1 ELSE 0 END) AS rlm_rows,
       SUM(CASE WHEN position(chr(8206) IN arabic_text) > 0 THEN 1 ELSE 0 END) AS lrm_rows
FROM quran_ayahs;

\echo Q9: every ayah has at least one translation
SELECT 'Q9.ayahs_no_translation' AS check, COUNT(*) AS violations
FROM quran_ayahs a
WHERE NOT EXISTS (
    SELECT 1 FROM quran_ayah_translations t WHERE t.verse_key = a.verse_key
);

\echo Q10: ayahs without chapter
SELECT 'Q10.orphan_ayahs' AS check, COUNT(*) AS violations
FROM quran_ayahs a
WHERE NOT EXISTS (
    SELECT 1 FROM quran_chapters c WHERE c.chapter_id = a.chapter_id
);

\echo Q11: ayah_translations without ayah
SELECT 'Q11.orphan_ayah_translations' AS check, COUNT(*) AS violations
FROM quran_ayah_translations t
WHERE NOT EXISTS (
    SELECT 1 FROM quran_ayahs a WHERE a.verse_key = t.verse_key
);

\echo Q12: RLM/LRM in Quran ayah translation text
SELECT 'Q12.translation_bidi' AS check,
       SUM(CASE WHEN position(chr(8207) IN translation_text) > 0 THEN 1 ELSE 0 END) AS rlm_rows,
       SUM(CASE WHEN position(chr(8206) IN translation_text) > 0 THEN 1 ELSE 0 END) AS lrm_rows
FROM quran_ayah_translations;

\echo Q13: replacement char in Quran text
SELECT 'Q13.quran_replacement_char' AS check,
       (SELECT COUNT(*) FROM quran_ayahs WHERE position(chr(65533) IN arabic_text) > 0) AS ayahs,
       (SELECT COUNT(*) FROM quran_ayah_translations WHERE position(chr(65533) IN translation_text) > 0) AS translations;

\echo Q14: NULL byte in Quran text
SELECT 'Q14.quran_null_byte' AS check,
       0 AS ayahs,
       0 AS translations;

\echo
\echo ##############################################################
\echo # SECTION 2 - DUA
\echo ##############################################################

\echo D1: 31 categories
SELECT 'D1.category_count' AS check, COUNT(*) AS actual, 31 AS expected,
       CASE WHEN COUNT(*) = 31 THEN 'PASS' ELSE 'FAIL' END AS status
FROM dua_categories;

\echo D2: every category has at least one item
SELECT 'D2.empty_categories' AS check, COUNT(*) AS violations
FROM dua_categories c
WHERE NOT EXISTS (
    SELECT 1 FROM dua_items i WHERE i.category = c.category
);

\echo D3: 72 dua items total
SELECT 'D3.item_count' AS check, COUNT(*) AS actual, 72 AS expected,
       CASE WHEN COUNT(*) = 72 THEN 'PASS' ELSE 'FAIL' END AS status
FROM dua_items;

\echo D4: every item has non-empty arabic_text
SELECT 'D4.empty_arabic' AS check, COUNT(*) AS violations
FROM dua_items
WHERE arabic_text IS NULL OR length(trim(arabic_text)) = 0;

\echo D5: every item has non-empty english_meaning
SELECT 'D5.empty_english' AS check, COUNT(*) AS violations
FROM dua_items
WHERE english_meaning IS NULL OR length(trim(english_meaning)) = 0;

\echo D6: items without category
SELECT 'D6.orphan_items' AS check, COUNT(*) AS violations
FROM dua_items i
WHERE NOT EXISTS (
    SELECT 1 FROM dua_categories c WHERE c.category = i.category
);

\echo D7: dua_categories.dua_count drift
SELECT 'D7.dua_count_drift' AS check, COUNT(*) AS violations
FROM dua_categories c
WHERE c.dua_count IS DISTINCT FROM (
    SELECT COUNT(*) FROM dua_items i WHERE i.category = c.category
);

\echo D8: RLM/LRM in Dua text
SELECT 'D8.dua_bidi' AS check,
       SUM(CASE WHEN position(chr(8207) IN arabic_text) > 0 THEN 1 ELSE 0 END) AS ar_rlm,
       SUM(CASE WHEN position(chr(8206) IN arabic_text) > 0 THEN 1 ELSE 0 END) AS ar_lrm,
       SUM(CASE WHEN position(chr(8207) IN english_meaning) > 0 THEN 1 ELSE 0 END) AS en_rlm,
       SUM(CASE WHEN position(chr(8206) IN english_meaning) > 0 THEN 1 ELSE 0 END) AS en_lrm
FROM dua_items;

\echo D9: replacement char in Dua text
SELECT 'D9.dua_replacement_char' AS check,
       SUM(CASE WHEN position(chr(65533) IN arabic_text) > 0 THEN 1 ELSE 0 END) AS ar,
       SUM(CASE WHEN position(chr(65533) IN english_meaning) > 0 THEN 1 ELSE 0 END) AS en
FROM dua_items;

\echo D10: NULL byte in Dua text
SELECT 'D10.dua_null_bytes' AS check,
       0 AS ar,
       0 AS en;

\echo
\echo ##############################################################
\echo # SECTION 3 - HADITH
\echo ##############################################################

\echo H1: cross-book chapter_ref_id misrouting
SELECT 'H1.cross_book' AS check, COUNT(*) AS violations
FROM hadith_items hi
JOIN hadith_chapters hc ON hc.id = hi.chapter_ref_id
WHERE hi.collection_name <> hc.collection_name
   OR hi.book_number <> hc.book_number;

\echo H2: hadith_items without chapter_ref_id
SELECT 'H2.orphan_hadiths' AS check, COUNT(*) AS violations
FROM hadith_items
WHERE chapter_ref_id IS NULL;

\echo H3: dangling chapter FK
SELECT 'H3.dangling_chapter_fk' AS check, COUNT(*) AS violations
FROM hadith_items hi
WHERE hi.chapter_ref_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM hadith_chapters hc WHERE hc.id = hi.chapter_ref_id
  );

\echo H4: chapter.hadith_count drift
SELECT 'H4.chapter_count_drift' AS check, COUNT(*) AS violations
FROM hadith_chapters hc
WHERE hc.hadith_count IS DISTINCT FROM (
    SELECT COUNT(*) FROM hadith_items hi WHERE hi.chapter_ref_id = hc.id
);

\echo H5: book.number_of_hadith drift
SELECT 'H5.book_count_drift' AS check, COUNT(*) AS violations
FROM hadith_books hb
WHERE hb.number_of_hadith IS DISTINCT FROM (
    SELECT COUNT(*) FROM hadith_items hi
    WHERE hi.collection_name = hb.collection_name
      AND hi.book_number = hb.book_number
);

\echo H6: collection.total_hadith drift
SELECT 'H6.collection_drift' AS check, COUNT(*) AS violations
FROM hadith_collections hcol
WHERE hcol.total_hadith IS DISTINCT FROM (
    SELECT COUNT(*) FROM hadith_items WHERE collection_name = hcol.name
);

\echo H7: duplicate collection/hadith_number
SELECT 'H7.dup_hadith_no' AS check, COUNT(*) AS violations
FROM (
    SELECT collection_name, hadith_number
    FROM hadith_items
    GROUP BY 1, 2
    HAVING COUNT(*) > 1
) t;

\echo H8: duplicate collection/book/chapter_order
SELECT 'H8.dup_chapter_order' AS check, COUNT(*) AS violations
FROM (
    SELECT collection_name, book_number, chapter_order
    FROM hadith_chapters
    GROUP BY 1, 2, 3
    HAVING COUNT(*) > 1
) t;

\echo H9: hadiths without localization
SELECT 'H9.no_localization' AS check, COUNT(*) AS violations
FROM hadith_items hi
WHERE NOT EXISTS (
    SELECT 1 FROM hadith_localizations hl WHERE hl.hadith_item_id = hi.id
);

\echo H10: RLM/LRM in Hadith Arabic body
SELECT 'H10.ar_bidi' AS check,
       SUM(CASE WHEN position(chr(8207) IN body_text) > 0 THEN 1 ELSE 0 END) AS rlm,
       SUM(CASE WHEN position(chr(8206) IN body_text) > 0 THEN 1 ELSE 0 END) AS lrm
FROM hadith_localizations
WHERE language = 'ar';

\echo H11: RLM/LRM in Hadith English body
SELECT 'H11.en_bidi' AS check,
       SUM(CASE WHEN position(chr(8207) IN body_text) > 0 THEN 1 ELSE 0 END) AS rlm,
       SUM(CASE WHEN position(chr(8206) IN body_text) > 0 THEN 1 ELSE 0 END) AS lrm
FROM hadith_localizations
WHERE language = 'en';

\echo H12: RLM/LRM in chapter title/intro fields
SELECT 'H12.chapter_bidi' AS check,
       SUM(CASE WHEN title_en IS NOT NULL AND (position(chr(8207) IN title_en) > 0 OR position(chr(8206) IN title_en) > 0) THEN 1 ELSE 0 END) AS title_en,
       SUM(CASE WHEN title_ar IS NOT NULL AND (position(chr(8207) IN title_ar) > 0 OR position(chr(8206) IN title_ar) > 0) THEN 1 ELSE 0 END) AS title_ar,
       SUM(CASE WHEN intro_en IS NOT NULL AND (position(chr(8207) IN intro_en) > 0 OR position(chr(8206) IN intro_en) > 0) THEN 1 ELSE 0 END) AS intro_en,
       SUM(CASE WHEN intro_ar IS NOT NULL AND (position(chr(8207) IN intro_ar) > 0 OR position(chr(8206) IN intro_ar) > 0) THEN 1 ELSE 0 END) AS intro_ar
FROM hadith_chapters;

\echo H13: hadith_number format by collection
SELECT 'H13.bad_format' AS check, hi.collection_name,
       SUM(CASE
           WHEN hi.collection_name = 'muslim'
                AND hi.hadith_number !~ '^[0-9]+([a-zA-Z]+)?(-[0-9]*[a-zA-Z]*)?(/[0-9]+([a-zA-Z]+)?)?$' THEN 1
           WHEN hi.collection_name <> 'muslim'
                AND hi.hadith_number !~ '^[0-9]+$' THEN 1
           ELSE 0
       END) AS bad
FROM hadith_items hi
GROUP BY hi.collection_name
ORDER BY hi.collection_name;

\echo H14: replacement char in Hadith body
SELECT 'H14.hadith_replacement_char' AS check,
       SUM(CASE WHEN position(chr(65533) IN body_text) > 0 THEN 1 ELSE 0 END) AS rows
FROM hadith_localizations;

\echo H15: NULL byte in Hadith body
SELECT 'H15.hadith_null_bytes' AS check,
       0 AS rows;

\echo H16: HTML entities in Hadith body
SELECT 'H16.html_entities_in_text' AS check,
       SUM(CASE WHEN body_text ~ '&(amp|lt|gt|quot|nbsp|#[0-9]+);' THEN 1 ELSE 0 END) AS body_rows
FROM hadith_localizations;

\echo H17: source-backed chapter titles are not placeholders
WITH normalized AS (
    SELECT
        lower(trim(coalesce(replace(replace(title_en, chr(8207), ''), chr(8206), ''), ''))) AS title_en_clean,
        trim(coalesce(replace(replace(title_ar, chr(8207), ''), chr(8206), ''), '')) AS title_ar_clean
    FROM hadith_chapters
    WHERE coalesce(hadith_count, 0) > 0
      AND coalesce(source_api, '') <> 'synthetic-catchall'
)
SELECT 'H17.chapter_title_placeholders' AS check,
       SUM(CASE WHEN title_en_clean IN (
           '',
           'chapter',
           'chapter:',
           'additional hadiths (not grouped by sunnah.com)',
           'introduction (unmapped preamble hadiths)'
       ) THEN 1 ELSE 0 END) AS title_en,
       SUM(CASE WHEN title_ar_clean IN ('', 'باب', 'باب:', 'باب :', '،', '.', ':') THEN 1 ELSE 0 END) AS title_ar
FROM normalized;

\echo H18: synthetic catch-all rows are isolated from source-backed chapters
SELECT 'H18.synthetic_catchall_inventory' AS check,
       collection_name,
       COUNT(*) AS chapters,
       SUM(hadith_count) AS hadiths
FROM hadith_chapters
WHERE source_api = 'synthetic-catchall'
GROUP BY collection_name
ORDER BY collection_name;

\echo H19: collection and book titles are real labels, not defaults
SELECT 'H19.collection_book_title_placeholders' AS check,
       (SELECT COUNT(*)
        FROM hadith_collections
        WHERE length(trim(coalesce(title, ''))) = 0
           OR lower(trim(title)) IN ('collection', 'unknown', 'default')) AS collections,
       (SELECT COUNT(*)
        FROM hadith_books
        WHERE length(trim(coalesce(title, ''))) = 0
           OR lower(trim(title)) IN ('book', 'unknown', 'default')
           OR title ~* '^(book|chapter) ?[0-9]*$') AS books;

\echo H20: Arabic localization is present for every Hadith item
SELECT 'H20.missing_ar_localization' AS check, COUNT(*) AS violations
FROM hadith_items hi
WHERE NOT EXISTS (
    SELECT 1
    FROM hadith_localizations hl
    WHERE hl.hadith_item_id = hi.id
      AND hl.language = 'ar'
      AND hl.body_text IS NOT NULL
      AND length(trim(hl.body_text)) > 0
);

\echo H21: source translation gaps inventory by language
SELECT 'H21.localization_gap_inventory' AS check,
       lang.language,
       COUNT(*) AS rows
FROM hadith_items hi
CROSS JOIN (VALUES ('ar'), ('en')) AS lang(language)
WHERE NOT EXISTS (
    SELECT 1
    FROM hadith_localizations hl
    WHERE hl.hadith_item_id = hi.id
      AND hl.language = lang.language
      AND hl.body_text IS NOT NULL
      AND length(trim(hl.body_text)) > 0
)
GROUP BY lang.language
ORDER BY lang.language;

\echo
\echo ##############################################################
\echo # SECTION 4 - FORMAT SCANS
\echo ##############################################################

\echo F1: other bidi controls U+202A..U+202E and U+2066..U+2069
WITH ranges AS (
    SELECT 8234 AS lo, 8238 AS hi
    UNION ALL
    SELECT 8294 AS lo, 8297 AS hi
)
SELECT 'F1.other_bidi_controls' AS check,
       (SELECT COUNT(*) FROM quran_ayahs a
        WHERE EXISTS (
            SELECT 1 FROM ranges r, generate_series(r.lo, r.hi) g
            WHERE position(chr(g) IN a.arabic_text) > 0
        )) AS quran_ayahs,
       (SELECT COUNT(*) FROM hadith_localizations hl
        WHERE EXISTS (
            SELECT 1 FROM ranges r, generate_series(r.lo, r.hi) g
            WHERE position(chr(g) IN hl.body_text) > 0
        )) AS hadith_localizations,
       (SELECT COUNT(*) FROM dua_items d
        WHERE EXISTS (
            SELECT 1 FROM ranges r, generate_series(r.lo, r.hi) g
            WHERE position(chr(g) IN d.arabic_text) > 0
        )) AS dua_items;

\echo F2: BOM prefix
SELECT 'F2.bom_prefix' AS check,
       (SELECT COUNT(*) FROM quran_ayahs WHERE arabic_text LIKE chr(65279) || '%' OR translation_text LIKE chr(65279) || '%') AS quran,
       (SELECT COUNT(*) FROM hadith_localizations WHERE body_text LIKE chr(65279) || '%') AS hadith,
       (SELECT COUNT(*) FROM dua_items WHERE arabic_text LIKE chr(65279) || '%' OR english_meaning LIKE chr(65279) || '%') AS dua;

\echo F3: whitespace-only text rows
SELECT 'F3.whitespace_only' AS check,
       (SELECT COUNT(*) FROM quran_ayahs WHERE arabic_text ~ '^[[:space:]]+$' OR translation_text ~ '^[[:space:]]+$') AS quran,
       (SELECT COUNT(*) FROM hadith_localizations WHERE body_text ~ '^[[:space:]]+$') AS hadith,
       (SELECT COUNT(*) FROM dua_items WHERE arabic_text ~ '^[[:space:]]+$') AS dua;

\echo F4: HTML tags in public text
SELECT 'F4.html_tags' AS check,
       (SELECT COUNT(*) FROM hadith_localizations WHERE body_text ~ '<[a-zA-Z/][^>]*>') AS hadith,
       (SELECT COUNT(*) FROM hadith_chapters WHERE intro_en ~ '<[a-zA-Z/][^>]*>' OR intro_ar ~ '<[a-zA-Z/][^>]*>') AS chapters,
       (SELECT COUNT(*) FROM dua_items WHERE arabic_text ~ '<[a-zA-Z/][^>]*>' OR english_meaning ~ '<[a-zA-Z/][^>]*>') AS dua;

\echo
\echo ##############################################################
\echo # COVERAGE SUMMARY
\echo ##############################################################

SELECT 'quran_chapters' AS table_name, COUNT(*) FROM quran_chapters UNION ALL
SELECT 'quran_ayahs', COUNT(*) FROM quran_ayahs UNION ALL
SELECT 'quran_translations', COUNT(*) FROM quran_translations UNION ALL
SELECT 'quran_ayah_translations', COUNT(*) FROM quran_ayah_translations UNION ALL
SELECT 'dua_categories', COUNT(*) FROM dua_categories UNION ALL
SELECT 'dua_items', COUNT(*) FROM dua_items UNION ALL
SELECT 'hadith_collections', COUNT(*) FROM hadith_collections UNION ALL
SELECT 'hadith_books', COUNT(*) FROM hadith_books UNION ALL
SELECT 'hadith_chapters', COUNT(*) FROM hadith_chapters UNION ALL
SELECT 'hadith_items', COUNT(*) FROM hadith_items UNION ALL
SELECT 'hadith_localizations', COUNT(*) FROM hadith_localizations UNION ALL
SELECT 'hadith_grades', COUNT(*) FROM hadith_grades
ORDER BY table_name;
