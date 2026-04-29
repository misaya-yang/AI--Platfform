from __future__ import annotations

from pathlib import Path

import pytest
from islamic_content_service.domain.constants import SCHEMA_VERSION
from islamic_content_service.migrations import MIGRATION_FILENAMES, apply_migrations


def test_migration_manifest_matches_sql_directory():
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    sql_files = tuple(path.name for path in sorted(migrations_dir.glob("*.sql")))

    assert sql_files == MIGRATION_FILENAMES
    assert "006_hadith_grades_unique.sql" in MIGRATION_FILENAMES
    assert MIGRATION_FILENAMES[-1].removesuffix(".sql") == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_apply_migrations_runs_every_manifest_entry_in_order():
    class FakeDb:
        def __init__(self) -> None:
            self.applied: list[str] = []

        async def migrate(self, script_path):
            self.applied.append(Path(script_path).name)

    db = FakeDb()

    await apply_migrations(db, migrations_dir="/tmp/islamic-migrations")

    assert tuple(db.applied) == MIGRATION_FILENAMES
