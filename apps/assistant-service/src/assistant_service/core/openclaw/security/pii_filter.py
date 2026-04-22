"""PII detection and redaction helper."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PIIFinding:
    """Detected sensitive pattern metadata."""

    pattern: str
    start: int
    end: int


class PIIFilter:
    """Regex-based redaction for sensitive user data before persistence."""

    PATTERNS: dict[str, re.Pattern[str]] = {
        "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "phone": re.compile(r"\b(?:\+?\d{1,2}[\s-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b"),
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "api_key": re.compile(r"\b(?:sk|gw)_[A-Za-z0-9\-_]{16,}\b"),
    }

    def find(self, text: str) -> list[PIIFinding]:
        findings: list[PIIFinding] = []
        for name, pattern in self.PATTERNS.items():
            for match in pattern.finditer(text or ""):
                findings.append(PIIFinding(pattern=name, start=match.start(), end=match.end()))
        findings.sort(key=lambda item: item.start)
        return findings

    def redact(self, text: str) -> tuple[str, list[PIIFinding]]:
        findings = self.find(text)
        if not findings:
            return text, []

        redacted = text
        for finding in reversed(findings):
            token = f"[REDACTED:{finding.pattern}]"
            redacted = redacted[: finding.start] + token + redacted[finding.end :]
        return redacted, findings
