"""End-to-end E2E — generate one real sample of each format + score them.

Runs the FULL pipeline (DocgenService → planner → renderer → verifier →
fix loop → artifact store) on four realistic prompts. Writes the final
signed-file copy into ``/Users/misaya.yanghejazfs.com.au/Desktop/test/test_word``
and prints:

  * file path
  * bytes
  * plan outline
  * critic rounds + pass/fail
  * structural vision-critic score (0-10)

The vision critic at this layer is deterministic (StructuralVisionCritic)
so this script is reproducible without burning an LLM call. Swap in
FreshContextVisionCritic(client_factory=...) in production.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from src.services.assistant.docgen.planners import Brief  # noqa: E402
from src.services.assistant.docgen.pipeline import DocgenPipeline  # noqa: E402
from src.services.assistant.docgen.quality.visual_verifier import StructuralVisionCritic  # noqa: E402
from src.services.assistant.docgen.quality.types import IssueSeverity  # noqa: E402


DEFAULT_OUT = Path("/Users/misaya.yanghejazfs.com.au/Desktop/test/test_word")


def _score(reports) -> tuple[float, str]:
    """Structural-critic pseudo-score 0-10 (not vision).

    Rules:
      start at 10. -3 per CRITICAL, -1 per WARNING, -0.2 per INFO.
      Clamp to [0, 10].
    """
    if not reports:
        return 10.0, "no critic"
    last = reports[-1]
    score = 10.0
    for i in last.issues:
        if i.severity == IssueSeverity.CRITICAL:
            score -= 3
        elif i.severity == IssueSeverity.WARNING:
            score -= 1
        else:
            score -= 0.2
    score = max(0.0, min(10.0, score))
    status = "pass" if last.passed else "fail"
    return score, status


def _make_briefs() -> dict[str, Brief]:
    return {
        "docx": Brief(
            doc_type="docx",
            title="Hejaz AI Gateway — Q2-2026 RAG Architecture Review",
            goal="Internal technical review of the RAG stack",
            body_markdown=(
                "# Executive Summary\n"
                "This document reviews the state of the Hejaz AI assistant-service's Retrieval-Augmented "
                "Generation pipeline as of 2026-Q2, covering ingestion, embedding, retrieval, re-ranking, "
                "and citation integrity.\n\n"
                "# Scorecard\n"
                "- Multilingual BM25: Q4-25 score 0, Q2-26 score 3, target 5.\n"
                "- Vision-critic loop: 0 → 2, target 4.\n"
                "- Citation integrity: 2 → 4, target 5.\n"
                "- Arabic tokenisation: 1 → 4, target 5.\n\n"
                "# Findings\n"
                "- Hybrid retrieval lifts top-5 accuracy from 71% to 88% across the KB.\n"
                "- Citation integrity regressed by 4 pp during the 0408 merge; rolled back.\n"
                "- Canary prompts (東京 π √ αβγ 测试) render correctly after Noto CJK inclusion.\n"
                "- Document generation still uses v1 python-docx — upgrade pending.\n\n"
                "# Plan\n"
                "1. Land the IR-driven renderer pipeline in the next sprint.\n"
                "2. Ship the vision-critic loop before the 2026-Q3 deployment.\n"
                "3. Roll out the artifact store and streaming UX together.\n\n"
                "# Appendix A — Key decisions\n"
                "- Pydantic IR is the planner ↔ renderer contract; no LLM emits raw OOXML.\n"
                "- Vision critic runs on a fresh Anthropic client — no shared context.\n"
                "- Sandbox-first: LibreOffice and Playwright never enter the API container.\n"
            ),
            palette_name="hejaz-green-gold",
            font_pair_name="helvetica-helvetica",
        ),
        "pptx": Brief(
            doc_type="pptx",
            title="Hejaz AI Assistant — Q2-2026 Quarterly Review",
            goal="Quarterly review deck for the engineering team and leadership",
            body_markdown=(
                "# Where we were\n"
                "Monolithic agent, single Qdrant collection, no critic.\n"
                "# What shipped\n"
                "Skill-driven planner, hybrid retrieval, pydantic IR.\n"
                "# Numbers\n"
                "24700\n"
                "# Four layers\n"
                "- Skill\n- Planner\n- Renderer\n- Verifier\n"
                "# Format coverage\n"
                "- DOCX via python-docx / docx-js\n"
                "- PPTX via pptxgenjs / python-pptx\n"
                "- XLSX via openpyxl + LibreOffice\n"
                "- PDF via ReportLab / Playwright / Typst\n"
                "# Close\n"
                "Render then verify, not verify then render.\n"
            ),
            palette_name="hejaz-green-gold",
            font_pair_name="helvetica-helvetica",
            style_hints={"chart": "true"},
        ),
        "xlsx": Brief(
            doc_type="xlsx",
            title="Hejaz FinanceModel 2026",
            goal="Three-year P&L with growth assumption and gross-profit formulas",
            palette_name="corporate-navy",
            font_pair_name="helvetica-helvetica",
            style_hints={"sheet_name": "P&L"},
        ),
        "pdf": Brief(
            doc_type="pdf",
            title="Hejaz AI — Document Generation SOTA Upgrade, One-page Brief",
            goal="Public one-page project brief for internal stakeholders report",
            body_markdown=(
                "# Why now\n"
                "The current document tools are 'it-runs' quality. python-docx for Word, "
                "python-pptx for PowerPoint, no real PDF engine, and no XLSX support at all. "
                "No vision critic. No verifier. No skill-driven planning.\n\n"
                "# What changes\n"
                "1. Skill-driven system prompt with progressive disclosure.\n"
                "2. Pydantic IR as the planner-to-renderer contract.\n"
                "3. Render-to-image plus fresh-context vision critic.\n"
                "4. Sandbox-first — no LibreOffice or Playwright in the API container.\n\n"
                "# Success metrics\n"
                "- Critic catch-rate: target ≥ 90%.\n"
                "- Two-pass pass-rate: target ≥ 95%.\n"
                "- User download rate: ~40% today, target ≥ 70%.\n\n"
                "# Roadmap\n"
                "Four phases, 6-8 weeks. Phase 0 = skill loader and sandbox. "
                "Phase 1 = IR and four renderers. Phase 2 = planner plus style guide. "
                "Phase 3 = vision critic and fix loop. Phase 4 = artifact store and streaming UX.\n"
            ),
            palette_name="hejaz-green-gold",
            font_pair_name="times-helvetica",
        ),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    pipeline = DocgenPipeline(
        llm=None,
        critic=StructuralVisionCritic(),  # deterministic critic for this run
        verify=True,
        max_fix_rounds=2,
    )

    briefs = _make_briefs()

    print(f"\nE2E RUN — writing to {out_dir}\n" + "=" * 74)
    summary = []
    for dt, brief in briefs.items():
        sub = out_dir / f".e2e_{dt}"
        sub.mkdir(parents=True, exist_ok=True)
        result = await pipeline.run(brief, sub)

        # Move the final artifact up to the visible out-dir.
        final = out_dir / result.path.name
        if final.exists():
            final.unlink()
        shutil.copy2(result.path, final)
        # Clean the intermediate dir.
        shutil.rmtree(sub, ignore_errors=True)

        score, status = _score(result.critic_reports)
        rounds = len(result.critic_reports)
        summary.append((dt, final, result, score, status, rounds))

        print(f"\n[{dt.upper()}] {final.name}")
        print(f"   path       : {final}")
        print(f"   bytes      : {result.bytes_size:,}")
        print(f"   plan (ms)  : {result.plan_ms}")
        print(f"   render (ms): {result.render_ms}")
        print(f"   llm used   : {result.used_llm}")
        print(f"   critic     : {rounds} round(s), final={status}")
        print(f"   score      : {score:.1f} / 10")
        print(f"   plan")
        for line in result.plan_text.splitlines():
            print(f"     {line}")

    print("\n" + "=" * 74)
    print(f"SUMMARY  (score / 10)")
    print("-" * 74)
    for dt, path, res, score, status, rounds in summary:
        print(f"  {dt:<5} {path.name:<62} {score:>4.1f}  [{status}]")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
