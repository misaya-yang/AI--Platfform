"""Tests for ``get_time_context_block``.

Guards against regressions where relative dates produced vague or repeated
searches. The block provides literal dates without arguing with the model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from assistant_service.core.prompts.system_prompt_v2 import get_time_context_block


def test_time_block_contains_iso_date_for_today() -> None:
    block = get_time_context_block()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    assert today_iso in block


def test_time_block_contains_explicit_yesterday_date() -> None:
    """The model must not have to do calendar arithmetic — provide the
    literal yesterday date so "yesterday" queries can be rewritten as
    dated web searches."""
    block = get_time_context_block()
    yesterday_iso = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert yesterday_iso in block


def test_time_block_contains_two_days_ago_date() -> None:
    block = get_time_context_block()
    two_days_ago_iso = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    assert two_days_ago_iso in block


def test_time_block_avoids_adversarial_meta_instructions() -> None:
    block = get_time_context_block()
    assert "do NOT" not in block
    assert "training cutoff" not in block


def test_time_block_states_how_relative_dates_are_used() -> None:
    block = get_time_context_block().lower()
    assert "relative-date" in block


def test_time_block_includes_web_search_guidance_with_dated_example() -> None:
    """The block should teach the model to put literal dates into
    time-sensitive web queries, not vague words like 'yesterday'."""
    block = get_time_context_block()
    # Some kind of search guidance is present. PR-2 dropped the
    # tool-name-specific phrasing, so we now match on the generic noun.
    assert "search" in block.lower() or "query" in block.lower()
    # Must show a literal YYYY-MM-DD somewhere as an example.
    assert re.search(r"\d{4}-\d{2}-\d{2}", block) is not None


def test_time_block_is_stable_string() -> None:
    """Sanity: two calls within the same second return identical strings
    — otherwise KV-cache behavior would be unpredictable. (This can flake
    across a midnight boundary, which is fine.)"""
    a = get_time_context_block()
    b = get_time_context_block()
    assert a == b
