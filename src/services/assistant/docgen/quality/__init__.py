"""Verifier + auto-fix layer.

The hard rule from the SOTA plan is: **no artifact leaves this layer
without at least one fix-and-verify round**.

Three verifier tracks:

  * PptxPdfVisualVerifier — soffice→pdftoppm→fresh-context vision critic.
    The critic runs against a fresh LLM client; the plan is explicit that
    it MUST NOT share context with the main agent.
  * XlsxFormulaVerifier — LibreOffice recalc, then openpyxl data_only to
    detect ``#REF!/#DIV/0!/#VALUE!/#N/A/#NAME?`` after formulas evaluate.
  * DocxXmlVerifier — OOXML unpack → lxml schema validate → placeholder
    grep → sanity reopen.

All verifiers return :class:`CriticReport`. When the dev environment is
missing ``soffice`` / pdftoppm we degrade to a local-structural pass —
the pipeline should still work on a laptop without LibreOffice.
"""

from .types import (
    CriticReport,
    Issue,
    IssueSeverity,
    IssueCategory,
)
from .visual_verifier import (
    FreshContextVisionCritic,
    PptxPdfVisualVerifier,
    StructuralVisionCritic,
    VisionCritic,
    default_vision_critic,
)
from .xlsx_verifier import XlsxFormulaVerifier
from .docx_verifier import DocxXmlVerifier
from .verifier_pipeline import VerifierPipeline

__all__ = [
    "CriticReport",
    "Issue",
    "IssueSeverity",
    "IssueCategory",
    "PptxPdfVisualVerifier",
    "VisionCritic",
    "FreshContextVisionCritic",
    "StructuralVisionCritic",
    "default_vision_critic",
    "XlsxFormulaVerifier",
    "DocxXmlVerifier",
    "VerifierPipeline",
]
