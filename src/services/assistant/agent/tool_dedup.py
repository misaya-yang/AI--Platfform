"""
Per-turn deduplication state for retrieval-style tool calls.

Prevents the model from re-issuing the same query within a single turn —
a common failure mode that wastes latency, burns the tool-iteration budget,
and inflates the prompt with redundant evidence.

Two independent de-dup trackers are exposed here:

* :class:`KBDedupState` — deduplicates ``search_knowledge_base`` calls. Because
  the KB is intended for a single focused query per turn, this tracker also
  short-circuits *any* second KB call, even with a different fingerprint.
* :class:`WebSearchDedupState` — deduplicates ``search_web`` / ``web_search``
  calls on a normalized query fingerprint. Unlike KB, multiple distinct web
  searches are legitimate — only near-duplicate queries are blocked.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field


# Canonical message injected when we short-circuit a duplicate KB call.
# Worded to steer the model toward answering with what it already has.
KB_REUSE_MESSAGE = (
    "Knowledge base has already been searched in this turn. "
    "Use the previously retrieved evidence to answer now; "
    "only call KB again if the user asks about a different topic."
)

# Canonical message injected when we short-circuit a duplicate web-search call.
# Mirrors KB_REUSE_MESSAGE — steer the model toward answering from what it
# already has, or toward a genuinely different query.
WEB_SEARCH_REUSE_MESSAGE = (
    "This web search query has already been executed in this turn. "
    "Use the previously retrieved results to answer now. "
    "If you truly need different evidence, issue a query with different "
    "keywords (not just reworded or translated)."
)

# Tool names that map to "web search". Keep the alias set in sync with the
# tool registry (see tools/builtin_tools.py).
_WEB_SEARCH_TOOL_NAMES = frozenset({"search_web", "web_search"})


def _normalize_query(query: str) -> str:
    """Normalize a query string for duplicate detection.

    Lowercase, strip, drop ASCII + common CJK punctuation, and collapse
    whitespace. This deliberately does *not* translate languages — the model
    switching between English and Chinese for "the same" query is usually
    intentional (different source domains), so we let those through.
    """
    if not query:
        return ""
    q = query.strip().lower()
    # Drop ASCII punctuation and common CJK full-width punctuation.
    drop_chars = string.punctuation + "，。、！？；：""''（）【】《》「」·—…"
    table = str.maketrans({ch: " " for ch in drop_chars})
    q = q.translate(table)
    # Collapse runs of whitespace.
    q = re.sub(r"\s+", " ", q).strip()
    return q


def web_query_fingerprint(arguments: dict) -> str:
    """Deterministic fingerprint for a web-search call.

    Normalizes the ``query`` argument (the only field that affects dedup).
    Returns an empty string if the query is missing or blank — an empty
    fingerprint never matches, so dedup becomes a no-op in that case.
    """
    if not isinstance(arguments, dict):
        return ""
    return _normalize_query(str(arguments.get("query") or ""))


def is_web_search_tool(tool_name: str) -> bool:
    """Whether ``tool_name`` is one of the web-search aliases."""
    return tool_name in _WEB_SEARCH_TOOL_NAMES


@dataclass
class KBDedupState:
    """Tracks which KB queries have fired this turn."""

    fingerprints_seen: set[str] = field(default_factory=set)
    search_completed: bool = False

    def should_skip(self, tool_name: str, fingerprint: str) -> tuple[bool, str | None]:
        """Decide whether this KB call should be short-circuited.

        Returns (skip, reason) where reason is one of:
          - "duplicate_fingerprint": same query + datasets already ran
          - "already_completed": first KB call in this turn already finished
          - None when not skipping.
        """
        if tool_name != "search_knowledge_base":
            return False, None
        if fingerprint and fingerprint in self.fingerprints_seen:
            return True, "duplicate_fingerprint"
        if self.search_completed:
            return True, "already_completed"
        return False, None

    def mark_completed(self, fingerprint: str) -> None:
        """Record that a KB search has completed in this turn."""
        self.search_completed = True
        if fingerprint:
            self.fingerprints_seen.add(fingerprint)


@dataclass
class WebSearchDedupState:
    """Tracks which web-search queries have fired this turn.

    Unlike KB (which is meant to run at most once per turn), web search may
    legitimately fire multiple times with distinct queries. So we *only*
    block on fingerprint match — never on "first search already completed".
    """

    fingerprints_seen: set[str] = field(default_factory=set)

    def should_skip(self, tool_name: str, fingerprint: str) -> tuple[bool, str | None]:
        """Decide whether this web-search call should be short-circuited.

        Returns (skip, reason) where reason is:
          - "duplicate_query": same normalized query already ran this turn
          - None when not skipping.
        """
        if not is_web_search_tool(tool_name):
            return False, None
        if fingerprint and fingerprint in self.fingerprints_seen:
            return True, "duplicate_query"
        return False, None

    def mark_completed(self, fingerprint: str) -> None:
        """Record that a web search has completed in this turn."""
        if fingerprint:
            self.fingerprints_seen.add(fingerprint)
