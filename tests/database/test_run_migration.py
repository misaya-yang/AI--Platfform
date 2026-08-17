from __future__ import annotations

import sys
from types import ModuleType

import pytest

from database import cli, run_migration


def test_get_dsn_prefers_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://primary.example/test")
    monkeypatch.setenv("GATEWAY_DATABASE__DSN", "postgresql://legacy.example/test")

    assert run_migration.get_dsn() == "postgresql://primary.example/test"


def test_get_dsn_fails_closed_without_leaking_settings_error(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_DATABASE__DSN", raising=False)

    fake_settings = ModuleType("src.config.settings")

    class ExplodingSettings:
        def __init__(self):
            raise RuntimeError("postgresql://user:secret-value@db.example/private")

    fake_settings.Settings = ExplodingSettings
    monkeypatch.setitem(sys.modules, "src.config.settings", fake_settings)

    with pytest.raises(SystemExit) as exc_info:
        run_migration.get_dsn()

    assert exc_info.value.code == 2
    assert "secret-value" not in capsys.readouterr().err


def test_cli_get_dsn_prefers_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://primary.example/test")
    monkeypatch.setenv("GATEWAY_DATABASE__DSN", "postgresql://legacy.example/test")

    assert cli.get_dsn() == "postgresql://primary.example/test"


def test_cli_get_dsn_fails_closed_without_hardcoded_password(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_DATABASE__DSN", raising=False)

    fake_settings = ModuleType("src.config.settings")

    class ExplodingSettings:
        def __init__(self):
            raise RuntimeError("postgresql://user:secret-value@db.example/private")

    fake_settings.Settings = ExplodingSettings
    monkeypatch.setitem(sys.modules, "src.config.settings", fake_settings)

    with pytest.raises(SystemExit) as exc_info:
        cli.get_dsn()

    assert exc_info.value.code == 2
    captured = capsys.readouterr().err
    assert "secret-value" not in captured
    assert "postgres:postgres" not in captured


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://user:secret@db.example:5432/gateway",
            "postgresql://user:******@db.example:5432/gateway",
        ),
        (
            "postgresql://user:p@ss:word@db.example:5432/gateway",
            "postgresql://user:******@db.example:5432/gateway",
        ),
        (
            "postgresql://localhost:5432/gateway",
            "postgresql://localhost:5432/gateway",
        ),
    ],
)
def test_cli_mask_dsn_hides_password_with_special_characters(dsn, expected):
    assert cli.mask_dsn(dsn) == expected
    assert "p@ss:word" not in cli.mask_dsn(dsn)
    assert "secret" not in cli.mask_dsn(dsn)
