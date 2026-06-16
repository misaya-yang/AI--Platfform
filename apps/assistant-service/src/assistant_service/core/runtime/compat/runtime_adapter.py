"""Assistant runtime adapter for compatibility-mode integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..context.assembler import ContextAssemblerV2
from ..memory.indexer import MemoryIndexer
from ..memory.reflector import DailyMemoryReflector
from ..memory.retriever import HybridMemoryRetriever, MemorySearchHit
from ..memory.source_store import MemorySourceStore
from ..scheduler.job_runner import SchedulerJobRunner
from ..security.pii_filter import PIIFilter
from ..security.sandbox_resolver import SandboxResolver
from ..skills.registry import SkillRegistry


@dataclass
class AssistantRuntimeFeatures:
    """Feature switches for staged assistant runtime rollout."""

    memory_v2: bool = False
    context_v2: bool = False
    tool_policy_v2: bool = False
    skills: bool = False
    scheduler: bool = False
    failover_v2: bool = False


@dataclass
class MemoryProviderResult:
    """Memory context returned by runtime adapter."""

    snippets: list[MemorySearchHit]
    loaded_sources: int
    fallback_used: bool
    fallback_reason: str | None = None


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class AssistantRuntimeAdapter:
    """Bridge optional runtime components into the assistant loop."""

    def __init__(
        self,
        *,
        features: AssistantRuntimeFeatures,
        memory_store: MemorySourceStore,
        memory_indexer: MemoryIndexer,
        memory_retriever: HybridMemoryRetriever,
        reflector: DailyMemoryReflector,
        pii_filter: PIIFilter,
        scheduler: SchedulerJobRunner,
        skill_registry: SkillRegistry,
        sandbox_resolver: SandboxResolver,
    ) -> None:
        self.features = features
        self.memory_store = memory_store
        self.memory_indexer = memory_indexer
        self.memory_retriever = memory_retriever
        self.reflector = reflector
        self.pii_filter = pii_filter
        self.scheduler = scheduler
        self.skill_registry = skill_registry
        self.sandbox_resolver = sandbox_resolver

    @classmethod
    def from_env(
        cls,
        *,
        database: Any,
        vector_store: Any | None = None,
        embedder: Any | None = None,
        base_memory_dir: str | None = None,
    ) -> AssistantRuntimeAdapter:
        """Build runtime adapter with env-driven feature flags."""
        features = AssistantRuntimeFeatures(
            memory_v2=_env_flag("ASSISTANT_RUNTIME_MEMORY_V2", True),
            context_v2=_env_flag("ASSISTANT_RUNTIME_CONTEXT_V2", False),
            tool_policy_v2=_env_flag("ASSISTANT_RUNTIME_TOOL_POLICY_V2", False),
            skills=_env_flag("ASSISTANT_RUNTIME_SKILLS", False),
            scheduler=_env_flag("ASSISTANT_RUNTIME_SCHEDULER", False),
            failover_v2=_env_flag("ASSISTANT_RUNTIME_FAILOVER_V2", False),
        )

        memory_store = MemorySourceStore(base_memory_dir)
        return cls(
            features=features,
            memory_store=memory_store,
            memory_indexer=MemoryIndexer(
                database,
                vector_store=vector_store,
                embedder=embedder,
            ),
            memory_retriever=HybridMemoryRetriever(
                database,
                vector_store=vector_store,
                embedder=embedder,
            ),
            reflector=DailyMemoryReflector(),
            pii_filter=PIIFilter(),
            scheduler=SchedulerJobRunner(database),
            skill_registry=SkillRegistry(database),
            sandbox_resolver=SandboxResolver(),
        )

    @staticmethod
    def normalize_mode(mode: str | None) -> str:
        value = (mode or "compat").strip().lower()
        if value not in {"off", "compat", "full"}:
            return "compat"
        return value

    async def load_memory_context(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str,
        runtime_mode: str,
        memory_profile: str | None,
        max_results: int = 6,
    ) -> MemoryProviderResult:
        """Load hybrid memory snippets for request-time context injection."""
        mode = self.normalize_mode(runtime_mode)
        profile = (memory_profile or "basic").strip().lower()
        if mode == "off":
            return MemoryProviderResult(
                snippets=[],
                loaded_sources=0,
                fallback_used=True,
                fallback_reason="runtime_mode_off",
            )
        if profile == "off":
            return MemoryProviderResult(
                snippets=[],
                loaded_sources=0,
                fallback_used=True,
                fallback_reason="memory_profile_off",
            )
        if not self.features.memory_v2:
            return MemoryProviderResult(
                snippets=[],
                loaded_sources=0,
                fallback_used=True,
                fallback_reason="memory_v2_disabled",
            )

        docs = self.memory_store.read_recent_sources(
            tenant_id=tenant_id,
            user_id=user_id,
            include_long_term=True,
            include_reflections=True,
            now=datetime.now(timezone.utc),
        )

        loaded_sources = 0
        for doc in docs:
            try:
                await self.memory_indexer.index_source(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_path=doc.path,
                    source_type=doc.source_type,
                    content=doc.content,
                    metadata={"source_type": doc.source_type},
                    updated_at=doc.updated_at,
                )
                loaded_sources += 1
            except Exception:
                continue

        fallback_used = False
        fallback_reason: str | None = None
        try:
            snippets = await self.memory_retriever.search(
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                max_results=max_results,
            )
        except Exception as exc:
            snippets = []
            fallback_used = True
            fallback_reason = str(exc)

        return MemoryProviderResult(
            snippets=snippets,
            loaded_sources=loaded_sources,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def build_context_assembler(self, provider: str) -> ContextAssemblerV2:
        """Factory for V2 context assembler."""
        return ContextAssemblerV2(provider=provider)

    async def schedule_daily_reflection(
        self,
        *,
        tenant_id: str,
        user_id: str,
        timezone_offset_minutes: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        """Schedule daily reflection job if scheduler feature is enabled."""
        if not self.features.scheduler:
            return None
        return await self.scheduler.schedule_daily_reflection(
            tenant_id=tenant_id,
            user_id=user_id,
            timezone_offset_minutes=timezone_offset_minutes,
            payload=payload,
        )
