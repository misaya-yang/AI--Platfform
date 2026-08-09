"""Memory lifecycle helpers for Assistant runtime memory."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .scope import public_source_label

MEMORY_LIFECYCLE_SCHEMA_VERSION = "assistant-memory-lifecycle/v1"

_ALLOWED_SYNC_REASONS = {"succeeded"}
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)(ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|"
    r"reveal\s+.*secret|jailbreak)"
)
_SECRET_LIKE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
    r"\s*[:=]\s*[^,\s;}]+"
)


@dataclass
class MemoryThreatScan:
    """Bounded safety scan result for memory text."""

    prompt_injection: bool = False
    secret_like: bool = False
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_injection": self.prompt_injection,
            "secret_like": self.secret_like,
            "findings": list(self.findings),
        }


@dataclass
class MemoryWriteResult:
    """Result metadata for a memory source write."""

    path: str
    source_type: str
    written: bool
    duplicate: bool
    content_hash: str
    threat_scan: MemoryThreatScan
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": public_source_label(self.path),
            "source_type": self.source_type,
            "written": self.written,
            "duplicate": self.duplicate,
            "content_hash": self.content_hash,
            "threat_scan": self.threat_scan.to_dict(),
            "metadata": dict(self.metadata),
        }


def bounded_memory_text(value: Any, *, max_chars: int = 6000) -> str:
    """Normalize memory text without preserving prompt-breaking markup."""

    text = str(value or "").replace("</context>", "").replace("<context>", "")
    text = "".join(
        char
        for char in text
        if char == "\n" or char == "\t" or (char.isprintable() and char != "\x00")
    )
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[memory truncated]"
    return text


def memory_content_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def scan_memory_text(value: Any) -> MemoryThreatScan:
    text = str(value or "")
    findings: list[str] = []
    prompt_injection = bool(_PROMPT_INJECTION_RE.search(text))
    secret_like = bool(_SECRET_LIKE_RE.search(text))
    if prompt_injection:
        findings.append("prompt_injection_like_text")
    if secret_like:
        findings.append("secret_like_text")
    return MemoryThreatScan(
        prompt_injection=prompt_injection,
        secret_like=secret_like,
        findings=findings,
    )


def should_sync_turn_to_memory(
    terminal_envelope: dict[str, Any] | None,
    *,
    explicit_opt_in: bool = False,
) -> tuple[bool, str]:
    """Return whether a completed turn should be synced into durable memory."""

    if not terminal_envelope:
        return False, "terminal_envelope_missing"
    exit_reason = str(terminal_envelope.get("exit_reason") or "").strip().lower()
    status = str(terminal_envelope.get("status") or "").strip().lower()
    if exit_reason in _ALLOWED_SYNC_REASONS and status == "succeeded":
        return True, "explicit_opt_in_completed_turn" if explicit_opt_in else "completed_turn"
    if not exit_reason:
        return False, "terminal_exit_reason_missing"
    return False, f"terminal_exit_reason_{exit_reason}"


def memory_policy_enabled(
    *,
    memory_mode: str | None = None,
    memory_profile: str | None = None,
) -> bool:
    """Return whether user long-term memory is enabled by both policy gates.

    ``memory_mode`` is the user-visible request control while ``memory_profile``
    bounds the enabled memory capabilities. Either control may disable memory;
    callers must never let a model-selected tool argument re-enable it.
    """

    mode = str(memory_mode or "").strip().lower()
    profile = str(memory_profile or "").strip().lower()
    return mode != "off" and profile != "off"


def memory_hit_provenance(hit: Any) -> dict[str, Any]:
    """Build trace/UI-safe provenance for a memory retrieval hit."""

    metadata = getattr(hit, "metadata", None) or {}
    source_path = public_source_label(getattr(hit, "source_path", ""))
    recency = metadata.get("recency") or metadata.get("updated_at")
    return {
        "schema_version": MEMORY_LIFECYCLE_SCHEMA_VERSION,
        "source_type": str(getattr(hit, "source_type", "") or "unknown"),
        "source_id": str(metadata.get("source_id") or ""),
        "source_path": source_path,
        "chunk_id": str(getattr(hit, "chunk_id", "") or ""),
        "start_line": int(getattr(hit, "start_line", 0) or 0),
        "end_line": int(getattr(hit, "end_line", 0) or 0),
        "score": float(getattr(hit, "final_score", 0.0) or 0.0),
        "recency": recency,
        "untrusted": True,
        "trust": "untrusted_memory_data",
    }


def context_hash(messages: list[dict[str, Any]]) -> str:
    encoded = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def build_compaction_lineage(
    *,
    parent_messages: list[dict[str, Any]],
    child_messages: list[dict[str, Any]],
    summary_text: str,
    reason: str,
    turns_total: int,
    turns_kept: int,
    messages_summarized: int,
) -> dict[str, Any]:
    """Record parent/child context lineage after compaction."""

    parent_hash = context_hash(parent_messages)
    child_hash = context_hash(child_messages)
    return {
        "schema_version": MEMORY_LIFECYCLE_SCHEMA_VERSION,
        "compaction_id": f"cmp_{uuid.uuid4().hex[:16]}",
        "parent_context_hash": parent_hash,
        "child_context_hash": child_hash,
        "reason": reason,
        "turns_total": turns_total,
        "turns_kept": turns_kept,
        "messages_summarized": messages_summarized,
        "summary_hash": memory_content_hash(summary_text)[:16],
        "summary_provenance": {
            "source": "assistant_context_compaction",
            "created_at": time.time(),
            "untrusted": True,
            "trust": "generated_summary",
        },
    }


class MemoryProviderLifecycle:
    """Safe default provider lifecycle contract."""

    async def initialize(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def prefetch(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def sync_turn(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def on_session_end(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def on_session_switch(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def on_pre_compact(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop", "flush_required": False}

    async def on_memory_write(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop"}

    async def flush_pending(self, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "noop", "flushed": False}
