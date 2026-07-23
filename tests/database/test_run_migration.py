from __future__ import annotations

import sys
from types import ModuleType

import pytest

from database import run_migration


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
