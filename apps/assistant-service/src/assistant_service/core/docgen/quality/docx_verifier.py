"""DocxXmlVerifier — OOXML-level sanity checks.

Three checks that run without LibreOffice / pandoc:

  1. Zip structure: every .docx is a ZIP with ``word/document.xml`` inside.
     A broken zip = critical.
  2. XML well-formedness of the main document part.
  3. Placeholder grep: "xxxx", "lorem", "ipsum", "TODO", "[[", "<<".

Optional (when ``soffice`` is on the PATH): convert to PDF as a
sanity-open — if LibreOffice cannot open the file, it is broken.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from ai_gateway_core.logging import record_internal_exception
from lxml import etree

from .types import CriticReport, Issue, IssueCategory, IssueSeverity

_PLACEHOLDER_RE = re.compile(
    r"\b(xxxx|lorem ipsum|lorem|ipsum|todo|fixme|placeholder|\[\[|<<)\b", re.IGNORECASE
)


class DocxXmlVerifier:
    name = "docx_xml"

    def verify(self, path: Path) -> CriticReport:
        issues: list[Issue] = []
        backend_note = "zip+xml"
        if not path.is_file():
            issues.append(
                Issue(
                    category=IssueCategory.OPEN_ERROR,
                    severity=IssueSeverity.CRITICAL,
                    message=f"missing file: {path}",
                )
            )
            return CriticReport(
                passed=False, issues=issues, backend="docx_xml", note="file missing"
            )

        # Zip structure
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "word/document.xml" not in names:
                    issues.append(
                        Issue(
                            category=IssueCategory.SCHEMA_ERROR,
                            severity=IssueSeverity.CRITICAL,
                            message="missing word/document.xml",
                        )
                    )
                    return CriticReport(passed=False, issues=issues, backend="docx_xml")
                document_xml = z.read("word/document.xml")
        except zipfile.BadZipFile:
            issues.append(
                Issue(
                    category=IssueCategory.SCHEMA_ERROR,
                    severity=IssueSeverity.CRITICAL,
                    message="corrupt zip archive",
                )
            )
            return CriticReport(passed=False, issues=issues, backend="docx_xml")

        # Well-formed XML
        try:
            root = etree.fromstring(document_xml)
        except etree.XMLSyntaxError as e:
            issues.append(
                Issue(
                    category=IssueCategory.SCHEMA_ERROR,
                    severity=IssueSeverity.CRITICAL,
                    message=f"invalid XML: {e}",
                )
            )
            return CriticReport(passed=False, issues=issues, backend="docx_xml")

        # Collect all text runs to grep for placeholders
        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        texts = root.xpath("//w:t/text()", namespaces=namespaces)
        doc_text = "\n".join(texts)
        for match in _PLACEHOLDER_RE.finditer(doc_text):
            issues.append(
                Issue(
                    category=IssueCategory.PLACEHOLDER,
                    severity=IssueSeverity.WARNING,
                    message=f"placeholder detected: {match.group(0)!r}",
                    hint="replace with concrete content",
                )
            )

        # Optional sanity-open via soffice.
        sanity_open_ok = self._soffice_sanity_open(path)
        if sanity_open_ok is False:
            issues.append(
                Issue(
                    category=IssueCategory.OPEN_ERROR,
                    severity=IssueSeverity.CRITICAL,
                    message="LibreOffice could not open the .docx",
                )
            )
            backend_note = "zip+xml+soffice_open_failed"
        elif sanity_open_ok is True:
            backend_note = "zip+xml+soffice_open_ok"

        passed = not any(i.severity == IssueSeverity.CRITICAL for i in issues)
        return CriticReport(passed=passed, issues=issues, backend="docx_xml", note=backend_note)

    def _soffice_sanity_open(self, path: Path) -> bool | None:
        if shutil.which("soffice") is None and shutil.which("libreoffice") is None:
            return None
        bin_name = "soffice" if shutil.which("soffice") else "libreoffice"
        try:
            proc = subprocess.run(
                [
                    bin_name,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(path.parent),
                    str(path),
                ],
                timeout=30,
                capture_output=True,
            )
            if proc.returncode != 0:
                return False
            out = path.with_suffix(".pdf")
            return out.exists()
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.docgen.quality.docx_verifier.internal_failure", exc
            )
            return False
