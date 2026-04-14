-- 006: Add unique constraint on hadith_grades to prevent duplicate grades
-- from concurrent enrichment runs.

CREATE UNIQUE INDEX IF NOT EXISTS uq_hadith_grades_item_lang_grader
ON hadith_grades (hadith_item_id, language, graded_by);
