"""Showcase — render the SAME deck in every design system for side-by-side review.

Writes to DOCGEN_SHOWCASE_OUT_DIR or tmp/docgen/showcase/.

Usage:
    python3 scripts/docgen_pptx_showcase.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from src.services.assistant.docgen.planners import Brief  # noqa: E402
from src.services.assistant.docgen.pipeline import DocgenPipeline  # noqa: E402
from src.services.assistant.docgen.design_system import available_systems  # noqa: E402


BRIEF_BODY = (
    "# Where we were\n"
    "Monolithic agent, single Qdrant collection, no critic.\n\n"
    "# What shipped\n"
    "Skill-driven planner, hybrid retrieval, pydantic IR.\n\n"
    "# Numbers\n"
    "24700\n\n"
    "# Four layers\n"
    "- Skill\n- Planner\n- Renderer\n- Verifier\n\n"
    "# Format coverage\n"
    "- DOCX via python-docx / docx-js\n"
    "- PPTX via pptxgenjs / python-pptx\n"
    "- XLSX via openpyxl + LibreOffice\n"
    "- PDF via ReportLab / Playwright / Typst\n\n"
    "# Close\n"
    "Render then verify, not verify then render.\n"
)


async def main() -> int:
    out_root = Path(os.getenv("DOCGEN_SHOWCASE_OUT_DIR", REPO / "tmp/docgen/showcase"))
    out_root.mkdir(parents=True, exist_ok=True)
    pipeline = DocgenPipeline(llm=None, verify=False)

    systems = available_systems()
    print(f"Rendering the same deck in {len(systems)} design systems → {out_root}\n")
    for ds_name in systems:
        brief = Brief(
            doc_type="pptx",
            title=f"AI Gateway · Q2-2026 Review — {ds_name}",
            goal="Quarterly review",
            body_markdown=BRIEF_BODY,
            style_hints={
                "chart": "true",
                "eyebrow": "2026-Q2 QUARTERLY REVIEW",
                "design_system": ds_name,
            },
        )
        result = await pipeline.run(brief, out_root)
        print(f"  ✓ {ds_name:<10}  {result.path.name:<60}  {result.bytes_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
