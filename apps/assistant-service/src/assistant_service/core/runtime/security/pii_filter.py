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
        "phone": re.compile(
            r"(?<![A-Za-z0-9_-])(?:"
            r"(?:\+?86[\s-]?)?1[3-9]\d(?:[\s-]?\d{4}){2}|"
            r"(?:\+\d{1,2}[\s-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}"
            r")(?![A-Za-z0-9_-])"
        ),
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "api_key": re.compile(
            r"\b(?:"
            r"(?:sk|gw)_[A-Za-z0-9\-_]{16,}|"
            r"ghp_[A-Za-z0-9]{36}|"
            r"github_pat_[A-Za-z0-9_]{82}|"
            r"AKIA[0-9A-Z]{16}|"
            r"xox[A-Za-z]-[A-Za-z0-9-]{10,}"
            r")\b"
        ),
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
