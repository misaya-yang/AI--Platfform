"""Guard: the streaming smoother applied to DashScope / OpenAI-compat
in 2026-04-24 must stay wired.

Source-inspection test (no LLM required). The 2026-04-24 fix wrapped
content deltas from ``_stream_openai`` in ``_smooth_text_delta`` when
the chunk has no meta (tool_calls / usage / finish_reason / thinking
ride atomic). If a future refactor removes the async-for wrap, users
see DashScope Intl outputs arrive in 2 big bursts again — the exact
symptom the fix addressed. This test fails loudly in that case.
"""
from __future__ import annotations

import re
from pathlib import Path


_REGISTRY = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "assistant-service"
    / "src"
    / "assistant_service"
    / "core"
    / "models"
    / "model_registry.py"
)


def _source() -> str:
    assert _REGISTRY.exists(), f"model_registry.py missing at {_REGISTRY}"
    return _REGISTRY.read_text()


def test_stream_openai_invokes_smoother_for_content_deltas():
    src = _source()
    # Locate the body of _stream_openai. Grab ~180 lines after the def.
    m = re.search(r"async def _stream_openai\b", src)
    assert m, "_stream_openai not found — did someone rename it?"
    body = src[m.start() : m.start() + 9000]  # ample window

    # Must reference _smooth_text_delta inside the function body.
    assert "_smooth_text_delta(content)" in body, (
        "_stream_openai no longer invokes _smooth_text_delta on content — "
        "DashScope Intl streaming will regress to 2-burst output. "
        "Restore the `async for _sub in _smooth_text_delta(content):` block."
    )

    # Must guard the smoother with has_meta so tool_calls / finish_reason /
    # usage / thinking deltas stay atomic (ordering matters).
    assert "has_meta" in body, "smoother should be gated by has_meta flag"


def test_smooth_text_delta_helper_still_exists_and_is_async():
    src = _source()
    assert "async def _smooth_text_delta(" in src, (
        "_smooth_text_delta helper removed — Gemini Vertex streaming will "
        "also regress (_stream_google depends on the same helper)."
    )


def test_smoother_min_len_threshold_sane():
    """Sanity guard: if someone bumps _SMOOTHER_MIN_TEXT_LEN to a
    huge value, nearly nothing gets smoothed and we're back to the
    bursty feel. Keep it <= 16."""
    src = _source()
    m = re.search(r"_SMOOTHER_MIN_TEXT_LEN\s*=\s*(\d+)", src)
    assert m, "_SMOOTHER_MIN_TEXT_LEN constant missing"
    val = int(m.group(1))
    assert val <= 16, (
        f"_SMOOTHER_MIN_TEXT_LEN={val} is too high; smoother only fires on "
        f"chunks >= {val} chars, most DashScope frames will pass through "
        "unsplit and users perceive bursty output."
    )


def test_smoother_sleep_delay_sane():
    """Per-chunk sleep adds artificial latency. Keep it <= 40ms so the
    total smoother-added latency on a 400-char response stays under 2s."""
    src = _source()
    m = re.search(r"_SMOOTHER_DELAY_SECONDS\s*=\s*([\d.]+)", src)
    assert m, "_SMOOTHER_DELAY_SECONDS constant missing"
    val = float(m.group(1))
    assert val <= 0.040, (
        f"_SMOOTHER_DELAY_SECONDS={val}s is too long; 400-char chunks "
        f"would add {val * 100:.0f}s latency end-to-end."
    )
