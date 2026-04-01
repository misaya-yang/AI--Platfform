"""
Preference extraction utilities for assistant memory.

This module provides basic rule-based preference extraction from user messages.
For production use, consider implementing LLM-based extraction for more accurate results.

TODO: Implement LLM-based preference extraction for:
- Communication style (formal/casual)
- Detail level (brief/comprehensive)
- Domain-specific preferences
- Timezone and locale preferences
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

PREFERENCE_KEYS = frozenset({"language", "verbosity", "format", "tone"})


def _extract_first_match(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = (match.group(1) or "").strip()
        value = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", value).strip()
        if value:
            return value
    return None


def extract_preferences(text: str) -> dict[str, Any]:
    """
    Extract user preferences from message text using rule-based patterns.

    This is a basic implementation that detects common preference indicators.
    For more sophisticated extraction, consider using an LLM.

    Args:
        text: User message text to analyze

    Returns:
        Dictionary of detected preferences. May include:
        - language: "zh" | "en" | "ja" | etc.
        - format: "markdown" | "plain" | "code"
        - verbosity: "brief" | "detailed"
        - tone: "formal" | "casual"

    Example:
        >>> extract_preferences("请用中文回复，要详细一点")
        {"language": "zh", "verbosity": "detailed"}
    """
    prefs: dict[str, Any] = {}

    if not text or not isinstance(text, str):
        return prefs

    text_lower = text.lower()

    # Language detection patterns
    language_patterns = {
        "zh": [r"中文", r"用中文", r"请用中文", r"chinese"],
        "en": [r"英文", r"用英文", r"请用英文", r"in english", r"english please"],
        "ja": [r"日本語", r"日语", r"japanese"],
        "ko": [r"한국어", r"韩语", r"korean"],
    }

    for lang, patterns in language_patterns.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                prefs["language"] = lang
                break
        if "language" in prefs:
            break

    # Verbosity patterns
    if any(
        kw in text
        for kw in ["详细", "详尽", "完整", "详细一点", "more detail", "comprehensive", "in detail"]
    ):
        prefs["verbosity"] = "detailed"
    elif any(
        kw in text for kw in ["简短", "简洁", "简单", "简要", "brief", "short", "concise", "quick"]
    ):
        prefs["verbosity"] = "brief"

    # Format patterns
    if any(kw in text_lower for kw in ["markdown", "md格式", "md 格式"]):
        prefs["format"] = "markdown"
    elif any(kw in text_lower for kw in ["代码", "code", "代码格式"]):
        prefs["format"] = "code"
    elif any(kw in text_lower for kw in ["纯文本", "plain text", "plain"]):
        prefs["format"] = "plain"

    # Tone patterns
    if any(kw in text for kw in ["正式", "专业", "formal", "professional"]):
        prefs["tone"] = "formal"
    elif any(kw in text for kw in ["轻松", "随意", "casual", "friendly", "口语"]):
        prefs["tone"] = "casual"

    preferred_name = _extract_first_match(
        text,
        [
            r"(?:我的名字是|你可以叫我|叫我)\s*([^\s，。！？,.!?:：；;]{1,24})",
            r"我叫(?!什么)\s*([^\s，。！？,.!?:：；;]{1,24})",
            r"(?:my name is|call me|you can call me)\s+([A-Za-z][A-Za-z0-9_ -]{0,31})",
        ],
    )
    if preferred_name:
        prefs["preferred_name"] = preferred_name

    location = _extract_first_match(
        text,
        [
            r"我来自(?!哪里)\s*([^\s，。！？,.!?:：；;]{1,32})",
            r"(?:我住在|我人在)\s*([^\s，。！？,.!?:：；;]{1,32})",
            r"(?:i am from|i'm from|i live in)\s+([A-Za-z][A-Za-z0-9_\\- ,]{0,47})",
        ],
    )
    if location:
        prefs["location"] = location

    return prefs


def split_memory_updates(extracted: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split extracted data into structured preferences and user profile facts."""
    preference_updates = {
        key: value for key, value in extracted.items() if key in PREFERENCE_KEYS and value is not None
    }
    fact_updates = {
        f"profile:{key}": value
        for key, value in extracted.items()
        if key not in PREFERENCE_KEYS and value is not None
    }
    return preference_updates, fact_updates


def merge_preferences(
    existing: dict[str, Any] | None,
    new: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge new preferences with existing ones.

    New preferences take precedence over existing ones.

    Args:
        existing: Current preference dictionary (may be None)
        new: New preferences to merge

    Returns:
        Merged preference dictionary
    """
    if not existing:
        return new.copy() if new else {}

    merged = existing.copy()
    merged.update(new)
    return merged
