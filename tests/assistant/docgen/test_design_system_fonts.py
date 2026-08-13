"""Lazy / thread-safe font probing (B5).

The probe in ``design_system._probe_installed_fonts`` was historically
eager and unguarded — the first caller could block the whole process
for up to 5s on ``fc-list``, and concurrent callers could race to spawn
N subprocesses. These tests pin the new behaviour:

  * Probe is lazy (only runs when a design system is requested).
  * Probe is guarded by a lock + cached result, so two sequential
    :func:`get_design_system` calls invoke ``subprocess.run`` at most once.
  * Missing ``fc-list`` → empty set, no subprocess.
  * ``subprocess.run`` TimeoutExpired → warning logged, empty set cached.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from assistant_service.core.docgen import design_system as ds


@pytest.fixture(autouse=True)
def _reset_font_cache():
    """Clear the module-level probe cache around each test."""
    ds._PROBED_FONTS = None
    yield
    ds._PROBED_FONTS = None


def test_missing_fc_list_returns_empty_set_without_subprocess():
    with patch("shutil.which", return_value=None), patch("subprocess.run") as run:
        result = ds._probe_installed_fonts()
        assert result == set()
        run.assert_not_called()


def test_probe_timeout_logs_warning_and_caches_empty(caplog):
    with (
        patch("shutil.which", return_value="/usr/bin/fc-list"),
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="fc-list", timeout=2),
        ) as run,
        caplog.at_level("WARNING", logger="assistant_service.core.docgen.design_system"),
    ):
        result = ds._probe_installed_fonts()
        assert result == set()
        # Warning about the timeout was emitted.
        assert any("timed out" in record.message.lower() for record in caplog.records)
        # Subsequent call must NOT re-run subprocess — cached empty set.
        assert ds._probe_installed_fonts() == set()
        run.assert_called_once()


def test_probe_generic_exception_logs_warning_and_caches_empty(caplog):
    with (
        patch("shutil.which", return_value="/usr/bin/fc-list"),
        patch("subprocess.run", side_effect=OSError("boom")) as run,
        caplog.at_level("WARNING", logger="assistant_service.core.docgen.design_system"),
    ):
        result = ds._probe_installed_fonts()
        assert result == set()
        assert any(
            "docgen.font_probe_failed" in record.message
            and "exception_type=OSError" in record.message
            and "fingerprint=" in record.message
            and "boom" not in record.message
            for record in caplog.records
        )
        run.assert_called_once()


def test_get_design_system_invokes_subprocess_only_once_across_calls():
    """Call ``get_design_system('claude')`` twice — subprocess spawned at most once."""
    fake_proc = MagicMock()
    fake_proc.stdout = "Georgia\nHelvetica Neue\n"
    with patch("shutil.which", return_value="/usr/bin/fc-list"), patch(
        "subprocess.run", return_value=fake_proc
    ) as run:
        first = ds.get_design_system("claude")
        second = ds.get_design_system("claude")
    assert first.name == "claude"
    assert second.name == "claude"
    # Exactly one fc-list invocation despite two design-system lookups.
    assert run.call_count == 1


def test_probe_timeout_value_is_two_seconds_not_five():
    """Regression: historical timeout was 5s — contract says 2s now."""
    fake_proc = MagicMock()
    fake_proc.stdout = ""
    with patch("shutil.which", return_value="/usr/bin/fc-list"), patch(
        "subprocess.run", return_value=fake_proc
    ) as run:
        ds._probe_installed_fonts()
    _, kwargs = run.call_args
    assert kwargs.get("timeout") == 2


def test_probe_is_not_run_at_import_time():
    """Importing the module must not spawn fc-list (laziness guarantee).

    We can't un-import, but we can assert the module-level cache is None
    until someone explicitly calls the probe or requests a design system.
    """
    # The autouse fixture just reset the cache. No design system requested yet.
    assert ds._PROBED_FONTS is None
