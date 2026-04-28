"""
Per-turn deduplication state for retrieval-style tool calls.

Prevents the model from re-issuing the same query within a single turn —
a common failure mode that wastes latency, burns the tool-iteration budget,
and inflates the prompt with redundant evidence.

Currently exposes :class:`KBDedupState` for ``search_knowledge_base``. The
web-search dedup tracker was removed in PR-2 along with the in-tree
``search_web`` (Tavily) tool; capable models do their own search via native
APIs and ``web_fetch`` is the URL-fetch fallback for everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Canonical message injected when we short-circuit a duplicate KB call.
# Worded to steer the model toward answering with what it already has.
KB_REUSE_MESSAGE = (
    "Knowledge base has already been searched in this turn. "
    "Use the previously retrieved evidence to answer now; "
    "only call KB again if the user asks about a different topic."
)


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
