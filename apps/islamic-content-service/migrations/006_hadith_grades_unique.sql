-- 006: Add unique constraint on hadith_grades to prevent duplicate grades
-- from concurrent enrichment runs.

SET search_path TO __ISLAMIC_CONTENT_SCHEMA__, public;

DELETE FROM hadith_grades duplicate
USING hadith_grades keeper
WHERE duplicate.hadith_item_id = keeper.hadith_item_id
  AND duplicate.language = keeper.language
  AND COALESCE(duplicate.graded_by, '') = COALESCE(keeper.graded_by, '')
  AND duplicate.id > keeper.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_hadith_grades_item_lang_grader
ON hadith_grades (hadith_item_id, language, COALESCE(graded_by, ''));
