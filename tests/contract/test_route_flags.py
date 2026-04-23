"""Tests for the per-route proxy flag helper used by Phase 5b migration."""
from __future__ import annotations

import pytest

from src.api.v1._route_flags import proxied


def test_default_off(monkeypatch):
    monkeypatch.delenv("ASSISTANT_ROUTE_MODELS_PROXIED", raising=False)
    monkeypatch.delenv("ASSISTANT_ROUTES_PROXIED_DEFAULT", raising=False)
    assert proxied("MODELS") is False


def test_specific_flag_on(monkeypatch):
    monkeypatch.setenv("ASSISTANT_ROUTE_MODELS_PROXIED", "true")
    assert proxied("MODELS") is True


def test_specific_flag_variants(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("ASSISTANT_ROUTE_X_PROXIED", value)
        assert proxied("X") is True, f"variant {value!r} should enable"


def test_specific_off_overrides_global_default(monkeypatch):
    """Per-route OFF wins over global ON — safe default for one-off
    rollback on a misbehaving route."""
    monkeypatch.setenv("ASSISTANT_ROUTES_PROXIED_DEFAULT", "true")
    monkeypatch.setenv("ASSISTANT_ROUTE_RUNS_PROXIED", "false")
    assert proxied("MODELS") is True
    assert proxied("RUNS") is False


def test_name_normalisation(monkeypatch):
    """Hyphens / dots in logical names map to underscores in env var."""
    monkeypatch.setenv("ASSISTANT_ROUTE_IMAGE_TASK_PROXIED", "true")
    assert proxied("image-task") is True
    assert proxied("image.task") is True
    assert proxied("IMAGE_TASK") is True


def test_global_default_on(monkeypatch):
    monkeypatch.delenv("ASSISTANT_ROUTE_MODELS_PROXIED", raising=False)
    monkeypatch.setenv("ASSISTANT_ROUTES_PROXIED_DEFAULT", "true")
    assert proxied("MODELS") is True
    assert proxied("RUNS") is True


def test_malformed_values_fall_back_to_off(monkeypatch):
    monkeypatch.setenv("ASSISTANT_ROUTE_MODELS_PROXIED", "maybe")
    assert proxied("MODELS") is False
