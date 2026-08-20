"""Session-scoped reasoning option compatibility helpers.

Provider capabilities resolve option IDs later from the selected model profile.
This module intentionally knows no providers, model identifiers or token budgets.
"""

from __future__ import annotations

import re

DEFAULT_THINKING_LEVEL = "auto"
_OPTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_LEGACY_ALIASES = {
    "disabled": "off",
    "false": "off",
    "none": "off",
    "0": "off",
    "enabled": "auto",
    "on": "auto",
    "true": "auto",
    "1": "auto",
}


def normalize_thinking_level(raw: str | None) -> str:
    """Normalize a legacy thinking level into a model-profile option id."""

    key = str(raw or "").strip().lower()
    if not key:
        return DEFAULT_THINKING_LEVEL
    key = _LEGACY_ALIASES.get(key, key)
    return key if _OPTION_RE.fullmatch(key) else DEFAULT_THINKING_LEVEL


def resolve_session_thinking_level(
    *,
    requested: str | None,
    stored: str | None = None,
) -> str:
    if requested is not None and str(requested).strip():
        return normalize_thinking_level(requested)
    if stored is not None and str(stored).strip():
        return normalize_thinking_level(stored)
    return DEFAULT_THINKING_LEVEL


def session_thinking_persist_value(
    *,
    requested: str | None,
    stored: str | None,
    effective: str,
) -> str | None:
    if requested is None or not str(requested).strip():
        return None
    if effective == (stored or ""):
        return None
    return effective


def resolve_turn_thinking_level(*, requested: str | None, iteration: int) -> str:
    """Keep the session option stable across model/tool turns for cache reuse."""

    del iteration
    return resolve_session_thinking_level(requested=requested)


__all__ = [
    "DEFAULT_THINKING_LEVEL",
    "normalize_thinking_level",
    "resolve_session_thinking_level",
    "resolve_turn_thinking_level",
    "session_thinking_persist_value",
]
