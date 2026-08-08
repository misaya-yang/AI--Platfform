"""Prompt-safe envelope for content produced outside the trusted runtime."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from ai_gateway_core.security import redact_trace_text

EXTERNAL_CONTENT_SCHEMA_VERSION = "assistant-external-content/v1"

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_LINE_RE = re.compile(
    r"(?im)^(?P<indent>[ \t]*)(?P<role>system|developer|user|assistant|tool)\s*:\s*"
)
_SPECIAL_ROLE_RE = re.compile(r"(?i)<\|\s*(system|developer|user|assistant|tool)\s*\|>")
_SAFE_LABEL_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _safe_label(value: str, *, fallback: str) -> str:
    normalized = _SAFE_LABEL_RE.sub("_", str(value or "").strip())[:96]
    return normalized or fallback


def normalize_external_text(value: object) -> str:
    """Remove prompt control characters and neutralize fake chat roles."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHAR_RE.sub("�", text)
    text = _SPECIAL_ROLE_RE.sub(lambda match: f"[external-role:{match.group(1).lower()}]", text)
    text = _ROLE_LINE_RE.sub(
        lambda match: f"{match.group('indent')}[external-role:{match.group('role').lower()}] ",
        text,
    )
    return redact_trace_text(text)


@dataclass(frozen=True)
class ExternalContent:
    """Internal provenance envelope for untrusted prompt data."""

    content: str
    source: str
    scope: str = "request"
    source_id: str = ""
    untrusted: bool = True

    def normalized(self) -> ExternalContent:
        content = normalize_external_text(self.content)
        source = _safe_label(self.source, fallback="external")
        scope = _safe_label(self.scope, fallback="request")
        source_id = _safe_label(self.source_id, fallback="")
        if not source_id:
            digest_source = f"{source}\0{scope}\0{content}".encode()
            source_id = f"ext_{hashlib.sha256(digest_source).hexdigest()[:16]}"
        return ExternalContent(
            content=content,
            source=source,
            scope=scope,
            source_id=source_id,
            untrusted=True,
        )

    def receipt(self) -> dict[str, object]:
        item = self.normalized()
        return {
            "schema_version": EXTERNAL_CONTENT_SCHEMA_VERSION,
            "source": item.source,
            "source_id": item.source_id,
            "scope": item.scope,
            "untrusted": True,
            "content_chars": len(item.content),
            "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest()[:16],
        }

    def to_prompt_text(self) -> str:
        item = self.normalized()
        return json.dumps(
            {
                "schema_version": EXTERNAL_CONTENT_SCHEMA_VERSION,
                "source": item.source,
                "source_id": item.source_id,
                "scope": item.scope,
                "untrusted": True,
                "content": item.content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def envelope_external_content(
    content: object,
    *,
    source: str,
    scope: str = "request",
    source_id: str = "",
) -> str:
    return ExternalContent(
        content=str(content or ""),
        source=source,
        scope=scope,
        source_id=source_id,
    ).to_prompt_text()
