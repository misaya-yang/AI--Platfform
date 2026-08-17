"""Thinking is a session capability, not a text classifier or turn index.

Hidden provider CoT follows the caller's or session's level. User text and
loop iteration are never inspected. The capability defaults to low; off must
be explicit for Qwen.
"""

from __future__ import annotations

from typing import Final

THINKING_LEVELS: Final[frozenset[str]] = frozenset({"off", "low", "medium", "high"})
DEFAULT_THINKING_LEVEL: Final[str] = "low"

_ALIASES: Final[dict[str, str]] = {
    "disabled": "off",
    "false": "off",
    "none": "off",
    "0": "off",
    "enabled": "low",
    "on": "low",
    "true": "low",
    "1": "low",
    "min": "low",
    "minimal": "low",
    "med": "medium",
    "mid": "medium",
    "max": "high",
    "ultra": "high",
    "xhigh": "high",
}

_QWEN_BUDGET: Final[dict[str, int | None]] = {
    "off": None,
    "low": 256,
    "medium": 1024,
    "high": None,
}

_QWEN_THINKING_MARKERS: Final[tuple[str, ...]] = ("qwen3", "qwen-plus", "qwen-max", "qwen2.5")


def normalize_thinking_level(raw: str | None) -> str:
    """Map caller strings onto off/low/medium/high without silently disabling thought."""

    if raw is None:
        return DEFAULT_THINKING_LEVEL
    key = str(raw).strip().lower()
    if not key:
        return DEFAULT_THINKING_LEVEL
    mapped = _ALIASES.get(key, key)
    return mapped if mapped in THINKING_LEVELS else DEFAULT_THINKING_LEVEL


def uses_qwen_thinking_protocol(model_id: str) -> bool:
    """DashScope hybrid-thinking models need an explicit enable_thinking flag."""

    mid = (model_id or "").lower()
    return any(marker in mid for marker in _QWEN_THINKING_MARKERS)


def qwen_thinking_request(level: str) -> tuple[bool, int | None]:
    """Return (enable_thinking, thinking_budget) for a normalized level."""

    normalized = normalize_thinking_level(level)
    if normalized == "off":
        return False, None
    return True, _QWEN_BUDGET[normalized]


def resolve_session_thinking_level(
    *,
    requested: str | None,
    stored: str | None = None,
) -> str:
    """Prefer this request, then the session value, then low."""

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
    """Return the value to write to session config, or None if unchanged."""

    if requested is None or not str(requested).strip():
        return None
    if effective == (stored or ""):
        return None
    return effective


def resolve_turn_thinking_level(*, requested: str | None, iteration: int) -> str:
    """Honor the session/request level on every model call in the run.

    ``iteration`` is accepted for call-site compatibility and is not used.
    Raising thinking after tool turns is a session/user choice, not a
    harness heuristic.
    """

    del iteration
    return resolve_session_thinking_level(requested=requested)


def apply_qwen_thinking_fields(
    body: dict[str, object],
    model_id: str,
    thinking_level: str | None,
    *,
    token_field: str,
) -> None:
    """Write DashScope thinking fields. Off is explicit false, never omitted."""

    if not uses_qwen_thinking_protocol(model_id):
        return
    enabled, budget = qwen_thinking_request(normalize_thinking_level(thinking_level))
    body["enable_thinking"] = enabled
    if enabled and budget is not None:
        body["thinking_budget"] = budget
    if enabled and token_field not in body:
        body[token_field] = 16384
