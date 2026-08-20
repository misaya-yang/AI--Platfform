"""Cross-platform artifact filename normalization."""

from __future__ import annotations

import re
import unicodedata

# Windows rejects these device names as a file's stem (the part before the
# first dot) regardless of extension or case — ``CON.txt`` still addresses
# the console device.
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def safe_document_stem(title: str, *, fallback: str, max_length: int = 80) -> str:
    """Keep readable Unicode letters/numbers while blocking path syntax."""

    normalized = unicodedata.normalize("NFKC", str(title or "")).strip()
    characters = [
        char if char.isalnum() or char in {"_", "-", "."} else "_"
        for char in normalized
    ]
    compact = re.sub(r"_+", "_", "".join(characters)).strip("._-")
    stem = compact[:max_length].rstrip("._-") or fallback
    # The reserved-name check runs on the final stem: truncation or the
    # fallback can both produce a reserved head ("CONSOLE..." capped at 3).
    # Swapping the head's last character for "_" keeps the length cap intact.
    head, separator, tail = stem.partition(".")
    if head.upper() in _WINDOWS_RESERVED_DEVICE_NAMES:
        stem = f"{head[:-1]}_{separator}{tail}"
    return stem
