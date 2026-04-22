"""Document generation subsystem (SOTA upgrade).

See plans/Document-Generation-SOTA-Upgrade-Plan-2026-04-21.md.

Layers:
  skills/     — SKILL.md loader + router (progressive disclosure)
  ir/         — pydantic document IR (planner ↔ renderer contract)
  planners/   — LLM-driven IR producers, one per format
  renderers/  — IR → binary file (python-docx / pptx / openpyxl / reportlab)
  quality/    — render-to-image + vision critic, formula recalc, OOXML validate
  sandbox/    — isolated exec env for LibreOffice / Node / matplotlib
  storage/    — artifact persistence (local / S3 / OSS)
"""

__all__ = [
    "skills",
    "ir",
    "planners",
    "renderers",
    "quality",
    "sandbox",
    "storage",
]
