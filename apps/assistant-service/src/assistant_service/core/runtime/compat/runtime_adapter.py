"""Assistant runtime adapter for compatibility-mode integration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context.assembler import ContextAssemblerV2
from ..memory.indexer import MemoryIndexer
from ..memory.lifecycle import (
    MemoryProviderLifecycle,
    MemoryWriteResult,
    memory_hit_provenance,
    should_sync_turn_to_memory,
)
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
    provenance: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.provenance is None:
            self.provenance = [memory_hit_provenance(snippet) for snippet in self.snippets]


@dataclass
class MemoryTurnSyncResult:
    """Completed-turn durable memory sync result."""

    synced: bool
    skipped: bool
    reason: str
    write: MemoryWriteResult | None = None
    index_result: Any | None = None
    pii_findings: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "synced": self.synced,
            "skipped": self.skipped,
            "reason": self.reason,
            "write": self.write.to_dict() if self.write else None,
            "index_result": self.index_result.__dict__
            if hasattr(self.index_result, "__dict__")
            else self.index_result,
            "pii_findings": self.pii_findings or [],
        }


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
        lifecycle: MemoryProviderLifecycle | None = None,
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
        self.lifecycle = lifecycle or MemoryProviderLifecycle()

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
            lifecycle=MemoryProviderLifecycle(),
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
            await self.lifecycle.prefetch(
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                source_count=loaded_sources,
            )
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

    async def sync_turn_to_memory(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        user_message: str,
        assistant_message: str,
        terminal_envelope: dict[str, Any] | None,
        explicit_opt_in: bool = False,
    ) -> MemoryTurnSyncResult:
        """Sync only eligible completed turns into durable daily memory."""
        allowed, reason = should_sync_turn_to_memory(
            terminal_envelope,
            explicit_opt_in=explicit_opt_in,
        )
        if not allowed:
            return MemoryTurnSyncResult(synced=False, skipped=True, reason=reason)

        conversation_snapshot = (
            f"User: {str(user_message or '').strip()}\n\n"
            f"Assistant: {str(assistant_message or '').strip()}"
        )
        if len(conversation_snapshot) > 6000:
            conversation_snapshot = conversation_snapshot[:6000]
        redacted_text, findings = self.pii_filter.redact(conversation_snapshot)
        write = self.memory_store.append_daily_entry_result(
            tenant_id,
            user_id,
            redacted_text,
        )
        await self.lifecycle.on_memory_write(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            write=write.to_dict(),
        )

        source_content = Path(write.path).read_text(encoding="utf-8", errors="ignore")
        index_result = await self.memory_indexer.index_source(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=write.path,
            source_type=write.source_type,
            content=source_content,
            metadata={
                "run_id": (terminal_envelope or {}).get("run_id"),
                "session_id": session_id,
                "source_type": write.source_type,
                "memory_layer": "durable_daily",
                "terminal_exit_reason": (terminal_envelope or {}).get("exit_reason"),
                "pii_findings": [finding.pattern for finding in findings],
                "write": write.to_dict(),
            },
        )
        await self.lifecycle.sync_turn(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            terminal_envelope=terminal_envelope,
            write=write.to_dict(),
        )
        return MemoryTurnSyncResult(
            synced=True,
            skipped=False,
            reason="completed_turn",
            write=write,
            index_result=index_result,
            pii_findings=[finding.pattern for finding in findings],
        )

    async def on_pre_compact(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        run_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Flush provider-side memory state before context compaction."""
        try:
            hook_result = await self.lifecycle.on_pre_compact(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                reason=reason,
            )
            flush_result = await self.lifecycle.flush_pending(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                reason=reason,
            )
            return {
                "status": "ok",
                "hook": hook_result,
                "flush": flush_result,
            }
        except Exception as exc:
            return {
                "status": "failed",
                "flushed": False,
                "reason": str(exc)[:200],
            }

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
