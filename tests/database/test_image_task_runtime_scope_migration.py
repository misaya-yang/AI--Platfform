from __future__ import annotations

from pathlib import Path


def test_image_task_scope_migration_is_additive_and_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    sql = (root / "database/migrations/097_image_task_runtime_scope.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS tenant_id" in sql
    assert "ADD COLUMN IF NOT EXISTS user_id" in sql
    assert "runtime_scope_version SMALLINT NOT NULL DEFAULT 0" in sql
    assert "runtime_scope_version = 1" in sql
    assert "tenant_id IS NOT NULL" in sql
    assert "user_id IS NOT NULL" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
