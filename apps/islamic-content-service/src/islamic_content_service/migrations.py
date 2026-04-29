from __future__ import annotations

from pathlib import Path
from typing import Protocol

MIGRATION_FILENAMES: tuple[str, ...] = (
    "001_init_schema.sql",
    "002_dua_tables.sql",
    "003_wahda_features.sql",
    "004_share_conversations.sql",
    "005_recommended_questions.sql",
    "006_hadith_grades_unique.sql",
    "007_hadith_chapters.sql",
    "008_strip_bidi_marks_arabic.sql",
    "009_fix_cross_book_hadith_linkage.sql",
    "010_drop_phantom_hadiths_and_recount.sql",
    "011_drop_empty_books.sql",
    "012_strip_replacement_and_advanced_bidi.sql",
    "013_strip_bidi_quran_translations.sql",
)


class MigrationDatabase(Protocol):
    async def migrate(self, script_path: str | Path) -> None: ...


def get_migrations_dir() -> Path:
    src_relative = Path(__file__).resolve().parents[2] / "migrations"
    return src_relative if src_relative.is_dir() else Path("/app/migrations")


async def apply_migrations(
    db: MigrationDatabase,
    migrations_dir: str | Path | None = None,
) -> None:
    root = Path(migrations_dir) if migrations_dir is not None else get_migrations_dir()
    for filename in MIGRATION_FILENAMES:
        await db.migrate(root / filename)
