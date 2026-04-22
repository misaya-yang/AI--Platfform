"""DocgenService — the one class the assistant agent / API routes talk to.

Combines:
  * SkillRouter         (progressive-disclosure system prompt)
  * DocgenPipeline      (planner → IR → renderer → verifier)
  * TemplateRegistry    (brand templates)
  * ArtifactStore       (persistence + signed URLs + tenant isolation)

The service is intentionally thin. Business services instantiate it with
whatever backends they need and call :meth:`generate` / :meth:`stream`.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from .pipeline import DocgenEvent, DocgenPipeline, DocgenResult
from .planners import Brief
from .planners.docx_planner import LLMCaller
from .quality import VisionCritic
from .skills import SkillRegistry, SkillRouter
from .skills.router import Intent
from .storage import Artifact, ArtifactStore, LocalArtifactStore
from .templates import Template, TemplateRegistry, default_registry


_DEFAULT_SKILL_PATHS = (
    Path(__file__).resolve().parent / "_skills_data",
)


@dataclass
class DocgenResponse:
    """User-facing response. The artifact id + signed URL are the two fields
    the front-end actually renders; the rest is for observability."""

    artifact: Artifact
    download_url: str
    plan_text: str
    passed: bool
    used_llm: bool
    render_ms: int
    plan_ms: int
    critic_summary: list[dict] = field(default_factory=list)


@dataclass
class GenerateRequest:
    tenant_id: str
    session_id: str
    turn_id: str
    doc_type: str
    title: str
    goal: str
    locale: str = "en-US"
    body_markdown: Optional[str] = None
    template_name: Optional[str] = None        # if set, theme is derived from template
    palette_name: Optional[str] = None
    font_pair_name: Optional[str] = None
    style_hints: Optional[dict] = None


class DocgenService:
    def __init__(
        self,
        *,
        artifact_store: Optional[ArtifactStore] = None,
        llm: Optional[LLMCaller] = None,
        critic: Optional[VisionCritic] = None,
        templates: Optional[TemplateRegistry] = None,
        skill_paths: Optional[list[Path]] = None,
        verify: bool = True,
        max_fix_rounds: int = 2,
    ) -> None:
        self._store = artifact_store or LocalArtifactStore(Path(tempfile.gettempdir()) / "docgen_artifacts")
        self._pipeline = DocgenPipeline(llm=llm, critic=critic, verify=verify, max_fix_rounds=max_fix_rounds)
        self._templates = templates or default_registry()

        paths = list(skill_paths or _DEFAULT_SKILL_PATHS)
        self._skill_registry = SkillRegistry(paths)
        self._skill_registry.scan()
        self._skill_router = SkillRouter(self._skill_registry)

    # -------------------------------------------------------------- skills

    def system_prompt_stub(self) -> str:
        """Text block to inject into the parent agent's system prompt."""
        return self._skill_router.system_prompt_stub()

    def expand_skill(self, doc_type: str, *, resources: Optional[list[str]] = None) -> str:
        skill = self._skill_router.select(Intent(doc_type=doc_type))
        if skill is None:
            return ""
        return self._skill_router.expand(skill, include_resources=resources)

    # -------------------------------------------------------------- generate

    async def generate(self, req: GenerateRequest) -> DocgenResponse:
        brief = self._brief_from_request(req)
        with tempfile.TemporaryDirectory(prefix="docgen_") as tmp:
            out_dir = Path(tmp)
            result = await self._pipeline.run(brief, out_dir)
            artifact = await self._persist(req, result)
            url = await self._store.download_url(artifact.artifact_id, tenant_id=req.tenant_id)
            return self._response(artifact, url, result)

    async def stream(self, req: GenerateRequest) -> AsyncIterator[DocgenEvent]:
        """SSE-style generator.

        Emits: plan → ir → render → (critic × N) → (fix) → artifact → done.
        The extra ``artifact`` event carries the signed URL; the caller can
        render a 'download' button as soon as it arrives.
        """
        brief = self._brief_from_request(req)
        with tempfile.TemporaryDirectory(prefix="docgen_") as tmp:
            out_dir = Path(tmp)
            last_path: Optional[Path] = None
            passed = True
            async for event in self._pipeline.run_streaming(brief, out_dir):
                if event.event == "render":
                    last_path = Path(event.data["path"])
                if event.event == "done":
                    passed = bool(event.data.get("passed", True))
                yield event
            if last_path and last_path.exists():
                artifact = await self._store.put(
                    last_path,
                    tenant_id=req.tenant_id,
                    session_id=req.session_id,
                    turn_id=req.turn_id,
                    doc_type=req.doc_type,
                    critic_passed=passed,
                )
                url = await self._store.download_url(artifact.artifact_id, tenant_id=req.tenant_id)
                yield DocgenEvent(event="artifact", data={
                    "artifact_id": artifact.artifact_id,
                    "url": url,
                    "size": artifact.size_bytes,
                    "filename": artifact.filename,
                    "sha256": artifact.sha256,
                })

    # -------------------------------------------------------------- helpers

    def _brief_from_request(self, req: GenerateRequest) -> Brief:
        palette = req.palette_name
        font_pair = req.font_pair_name
        if req.template_name:
            try:
                tmpl = self._templates.get(req.template_name, tenant_id=req.tenant_id)
                palette = tmpl.palette_name
                font_pair = tmpl.font_pair_name
            except KeyError:
                pass
        return Brief(
            doc_type=req.doc_type,
            title=req.title,
            goal=req.goal,
            locale=req.locale,
            body_markdown=req.body_markdown,
            palette_name=palette,
            font_pair_name=font_pair,
            style_hints=req.style_hints or {},
        )

    async def _persist(self, req: GenerateRequest, result: DocgenResult) -> Artifact:
        critic_summary = []
        for r in result.critic_reports:
            critic_summary.append({
                "backend": r.backend,
                "passed": r.passed,
                "critical_count": r.critical_count,
                "issues": [i.to_dict() for i in r.issues][:5],
            })
        return await self._store.put(
            result.path,
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            turn_id=req.turn_id,
            doc_type=req.doc_type,
            critic_passed=result.passed,
            extra={"plan_text": result.plan_text, "critic_rounds": len(result.critic_reports), "critic_summary": critic_summary},
        )

    def _response(self, artifact: Artifact, download_url: str, result: DocgenResult) -> DocgenResponse:
        critic_summary = artifact.extra.get("critic_summary", []) if artifact.extra else []
        return DocgenResponse(
            artifact=artifact,
            download_url=download_url,
            plan_text=result.plan_text,
            passed=result.passed,
            used_llm=result.used_llm,
            render_ms=result.render_ms,
            plan_ms=result.plan_ms,
            critic_summary=critic_summary,
        )
