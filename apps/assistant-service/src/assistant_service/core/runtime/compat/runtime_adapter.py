"""Assistant runtime adapter for compatibility-mode integration."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_gateway_core.agent_plugins import AgentPluginLoadError, load_agent_plugin
from ai_gateway_core.logging import record_internal_exception

from ..context.assembler import ContextAssemblerV2
from ..memory.indexer import MemoryIndexer
from ..memory.lifecycle import (
    MemoryProviderLifecycle,
    memory_hit_provenance,
)
from ..memory.reflector import DailyMemoryReflector
from ..memory.retriever import HybridMemoryRetriever, MemorySearchHit
from ..memory.scope import public_source_label
from ..memory.source_store import MemorySourceStore
from ..memory.turn_sync import CompletedTurnMemorySync, MemoryTurnSyncResult
from ..scheduler.job_runner import SchedulerJobRunner
from ..security.pii_filter import PIIFilter
from ..security.sandbox_resolver import SandboxResolver
from ..skills.registry import SkillRegistry

if TYPE_CHECKING:
    from ...agent.plugin_catalog import AgentPluginCatalog

logger = logging.getLogger(__name__)


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
class MemoryDeleteResult:
    """Cross-store deletion receipt with explicit read-back state."""

    status: str
    completed: bool
    retryable: bool
    source_found: bool
    source_label: str
    file_status: str
    index: dict[str, Any]
    read_back: dict[str, Any]
    source_id: str = ""
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "completed": self.completed,
            "partial": self.status == "partial",
            "retryable": self.retryable,
            "source_found": self.source_found,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "file_status": self.file_status,
            "index": dict(self.index),
            "read_back": dict(self.read_back),
            "errors": list(self.errors),
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
        agent_plugin_catalog: AgentPluginCatalog | None = None,
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
        self.memory_turn_sync = CompletedTurnMemorySync(
            memory_store=self.memory_store,
            memory_indexer=self.memory_indexer,
            pii_filter=self.pii_filter,
            lifecycle=self.lifecycle,
        )
        self.agent_plugin_status: list[dict[str, Any]] = []
        # Compatibility view only: discovery belongs to the process-scoped,
        # DB-independent catalog built by the composition root.
        self.agent_plugin_catalog = agent_plugin_catalog
        self.agent_plugin_agents = list(agent_plugin_catalog.agents) if agent_plugin_catalog else []

    def _load_configured_agent_plugins(self) -> None:
        """Load operator-selected Skills without granting plugin authority."""

        raw_paths = os.getenv("ASSISTANT_AGENT_PLUGIN_PATHS", "")
        if not self.features.skills or not raw_paths.strip():
            return

        loaded_plugins = 0
        loaded_skills = 0
        seen_paths: set[Path] = set()
        for raw_path in raw_paths.split(os.pathsep):
            if not raw_path.strip():
                continue
            candidate = Path(raw_path).expanduser()
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                self.agent_plugin_status.append(
                    {"status": "rejected", "code": "AGENT_PLUGIN_ROOT_INVALID"}
                )
                logger.warning("agent_plugin.rejected code=AGENT_PLUGIN_ROOT_INVALID")
                continue
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            try:
                package = load_agent_plugin(resolved)
            except AgentPluginLoadError as exc:
                self.agent_plugin_status.append({"status": "rejected", "code": exc.code})
                record_internal_exception(
                    logger, "agent_plugin.runtime_load_rejected", exc, level=logging.WARNING
                )
                continue

            registered: list[str] = []
            diagnostics = [item.to_dict() for item in package.diagnostics]
            for skill in package.skills:
                if self.skill_registry.get(skill.name) is not None:
                    diagnostics.append(
                        {
                            "status": "warning",
                            "code": "AGENT_PLUGIN_SKILL_NAME_CONFLICT",
                            "component": skill.name,
                        }
                    )
                    continue
                self.skill_registry.register(skill)
                registered.append(skill.name)
                loaded_skills += 1
            loaded_plugins += 1
            self.agent_plugin_status.append(
                {
                    "status": "loaded",
                    "plugin": package.manifest.name,
                    "skills": registered,
                    "mcp_supported": bool(package.mcp_servers),
                    "mcp_servers": [server.name for server in package.mcp_servers],
                    "diagnostics": diagnostics,
                }
            )
        logger.info(
            "agent_plugins.loaded plugins=%s skills=%s",
            loaded_plugins,
            loaded_skills,
        )

    @classmethod
    def from_env(
        cls,
        *,
        database: Any,
        vector_store: Any | None = None,
        embedder: Any | None = None,
        base_memory_dir: str | None = None,
        legacy_memory_dir: str | None = None,
        memory_max_source_bytes: int | None = None,
        agent_plugin_catalog: AgentPluginCatalog | None = None,
        features: AssistantRuntimeFeatures | None = None,
    ) -> AssistantRuntimeAdapter:
        """Build runtime adapter with env-driven feature flags."""
        resolved_features = features or AssistantRuntimeFeatures(
            memory_v2=_env_flag("ASSISTANT_RUNTIME_MEMORY_V2", True),
            context_v2=_env_flag("ASSISTANT_RUNTIME_CONTEXT_V2", True),
            tool_policy_v2=_env_flag("ASSISTANT_RUNTIME_TOOL_POLICY_V2", False),
            skills=_env_flag("ASSISTANT_RUNTIME_SKILLS", True),
            scheduler=_env_flag("ASSISTANT_RUNTIME_SCHEDULER", False),
            failover_v2=_env_flag("ASSISTANT_RUNTIME_FAILOVER_V2", False),
        )

        configured_memory_dir = (
            base_memory_dir
            if base_memory_dir is not None
            else os.getenv("ASSISTANT_RUNTIME_MEMORY_DIR")
        )
        memory_store = MemorySourceStore(
            configured_memory_dir,
            legacy_base_dir=legacy_memory_dir,
            max_source_bytes=memory_max_source_bytes,
        )
        adapter = cls(
            features=resolved_features,
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
            agent_plugin_catalog=agent_plugin_catalog,
        )
        adapter._load_configured_agent_plugins()
        return adapter

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
            if self.memory_turn_sync.source_is_pending(tenant_id, user_id, doc.path):
                continue
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
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                continue

        # Old releases used a lossy tenant/user directory sanitizer.  Never
        # enumerate that tree directly: SQL must first prove that the stored
        # source path is referenced by exactly this scope.
        legacy_records: list[dict[str, Any]] = []
        list_records = getattr(self.memory_indexer, "list_scoped_source_records", None)
        if list_records is not None:
            try:
                legacy_records = await list_records(
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
        for record in legacy_records:
            try:
                document, source_handle = self.memory_store.read_legacy_source_document(
                    tenant_id,
                    user_id,
                    str(record.get("source_path") or ""),
                    source_type=str(record.get("source_type") or "unknown"),
                    owner_proven=record.get("owner_proven") is True,
                )
                await self.memory_indexer.index_source(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_path=str(record.get("source_path") or ""),
                    source_type=document.source_type,
                    content=document.content,
                    metadata={
                        "source_type": document.source_type,
                        "source_handle": source_handle,
                        "legacy_source": True,
                    },
                    updated_at=document.updated_at,
                )
                loaded_sources += 1
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
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
            record_internal_exception(
                __name__, "assistant.core.runtime.compat.runtime_adapter.internal_failure", exc
            )
            snippets = []
            fallback_used = True
            fallback_reason = "memory_retrieval_failed"

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
        """Commit a completed turn, then refresh derived stores off-path."""
        return await self.memory_turn_sync.sync(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            terminal_envelope=terminal_envelope,
            explicit_opt_in=explicit_opt_in,
        )

    async def flush_pending_memory_sync(
        self,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Wait for queued memory derivatives at explicit lifecycle barriers."""

        return await self.memory_turn_sync.flush_pending(timeout=timeout)

    def memory_sync_status(self, operation_id: str) -> dict[str, Any] | None:
        """Return a prompt-free background synchronization receipt."""

        return self.memory_turn_sync.status(operation_id)

    async def delete_memory_source(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_path: str,
        expected_source_handle: str | None = None,
        index_source_path: str | None = None,
        legacy_owner_proven: bool = False,
        expected_database_source_handle: str | None = None,
    ) -> MemoryDeleteResult:
        """Delete a scoped source using a retryable two-phase protocol."""

        if legacy_owner_proven:
            target = self.memory_store.resolve_legacy_owned_source(
                tenant_id,
                user_id,
                source_path,
                owner_proven=True,
            )
        else:
            target = self.memory_store.resolve_owned_source(
                tenant_id,
                user_id,
                source_path,
            )
        source_label = public_source_label(source_path)
        current_source_handle = self.memory_store.source_handle_for_path(
            tenant_id,
            user_id,
            source_path,
            legacy_owner_proven=legacy_owner_proven,
        )
        if target is None:
            return MemoryDeleteResult(
                status="rejected",
                completed=False,
                retryable=False,
                source_found=False,
                source_label=source_label,
                file_status="out_of_scope",
                index={},
                read_back={"file_absent": None},
                source_id="",
                errors=("memory_source_out_of_scope",),
            )

        expected_staged_exists = bool(
            expected_source_handle
            and self.memory_store.staged_source_exists(
                tenant_id,
                user_id,
                str(target),
                source_handle=expected_source_handle,
                legacy_owner_proven=legacy_owner_proven,
            )
        )
        if (
            expected_source_handle
            and target.exists()
            and not expected_staged_exists
            and current_source_handle != expected_source_handle
        ):
            return MemoryDeleteResult(
                status="conflict",
                completed=False,
                retryable=False,
                source_found=True,
                source_label=source_label,
                file_status="generation_changed",
                index={"status": "generation_conflict", "completed": False},
                read_back={
                    "file_absent": False,
                    "sql_source_absent": None,
                    "sql_chunks_absent": None,
                    "vector_points_remaining": None,
                },
                source_id=expected_source_handle,
                errors=("memory_source_generation_conflict",),
            )
        source_handle = expected_source_handle or current_source_handle
        file_existed = target.exists() and target.is_file()
        staged_exists = expected_staged_exists
        staged_by_this_call = False
        if source_handle and (file_existed or staged_exists):
            try:
                stage_status, _ = self.memory_store.stage_source_for_deletion(
                    tenant_id,
                    user_id,
                    str(target),
                    expected_source_handle=source_handle,
                    legacy_owner_proven=legacy_owner_proven,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                stage_status = "failed"
            staged_by_this_call = bool(
                stage_status == "staged" and file_existed and not expected_staged_exists
            )
            if stage_status in {"generation_conflict", "deletion_in_progress"}:
                return MemoryDeleteResult(
                    status="conflict",
                    completed=False,
                    retryable=False,
                    source_found=True,
                    source_label=source_label,
                    file_status=stage_status,
                    index={"status": "generation_conflict", "completed": False},
                    read_back={
                        "file_absent": False,
                        "sql_source_absent": None,
                        "sql_chunks_absent": None,
                        "vector_points_remaining": None,
                    },
                    source_id=source_handle,
                    errors=("memory_source_generation_conflict",),
                )
            if stage_status not in {"staged", "finalizing", "absent"}:
                return MemoryDeleteResult(
                    status="partial",
                    completed=False,
                    retryable=True,
                    source_found=file_existed or staged_exists,
                    source_label=source_label,
                    file_status=stage_status,
                    index={"status": "not_attempted", "completed": False},
                    read_back={
                        "file_absent": False,
                        "sql_source_absent": None,
                        "sql_chunks_absent": None,
                        "vector_points_remaining": None,
                    },
                    source_id=source_handle,
                    errors=("memory_source_stage_failed",),
                )
        staged_exists = bool(
            source_handle
            and self.memory_store.staged_source_exists(
                tenant_id,
                user_id,
                str(target),
                source_handle=source_handle,
                legacy_owner_proven=legacy_owner_proven,
            )
        )
        errors: list[str] = []
        try:
            delete_index_kwargs: dict[str, Any] = {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "source_path": index_source_path or str(target),
                "source_handle": source_handle,
            }
            if expected_database_source_handle:
                delete_index_kwargs["expected_database_source_handle"] = (
                    expected_database_source_handle
                )
            prepare_result = await self.memory_indexer.delete_source_index(
                **delete_index_kwargs,
            )
            prepare_receipt = prepare_result.to_dict()
            errors.extend(prepare_result.errors)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.compat.runtime_adapter.internal_failure", exc
            )
            prepare_result = None
            prepare_receipt = {
                "status": "partial",
                "completed": False,
                "retryable": True,
                "errors": ["memory_index_delete_failed"],
            }
            errors.append("memory_index_delete_failed")

        if not (prepare_result and prepare_result.ready_for_source_unlink):
            restored_after_conflict = False
            if (
                prepare_result is not None
                and prepare_result.status == "conflict"
                and staged_by_this_call
                and source_handle
            ):
                try:
                    restore_status = self.memory_store.restore_staged_source(
                        tenant_id,
                        user_id,
                        str(target),
                        expected_source_handle=source_handle,
                        legacy_owner_proven=legacy_owner_proven,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                        exc,
                    )
                    restore_status = "failed"
                restored_after_conflict = restore_status == "restored"
                if restored_after_conflict:
                    staged_exists = False
                else:
                    errors.append("memory_source_restore_failed")
            return MemoryDeleteResult(
                status="partial",
                completed=False,
                retryable=True,
                source_found=file_existed or bool(prepare_receipt.get("source_found")),
                source_label=source_label,
                file_status=(
                    "restored_after_generation_conflict"
                    if restored_after_conflict
                    else "staged_for_retry"
                    if staged_exists
                    else "absent"
                ),
                index={"prepare": prepare_receipt},
                read_back={
                    "file_absent": not target.exists() and not staged_exists,
                    "sql_source_absent": prepare_receipt.get("sql_source_absent"),
                    "sql_chunks_absent": prepare_receipt.get("sql_chunks_absent"),
                    "vector_points_remaining": prepare_receipt.get("vector_points_remaining"),
                },
                source_id=source_handle or "",
                errors=tuple(dict.fromkeys(errors)),
            )

        file_status = "absent"
        if staged_exists and source_handle:
            try:
                file_status = self.memory_store.delete_staged_source(
                    tenant_id,
                    user_id,
                    str(target),
                    expected_source_handle=source_handle,
                    legacy_owner_proven=legacy_owner_proven,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                file_status = "failed"

        file_absent = not (
            source_handle
            and self.memory_store.staged_source_exists(
                tenant_id,
                user_id,
                str(target),
                source_handle=source_handle,
                legacy_owner_proven=legacy_owner_proven,
            )
        )
        if not file_absent:
            errors.append("memory_source_file_delete_failed")
            return MemoryDeleteResult(
                status="partial",
                completed=False,
                retryable=True,
                source_found=True,
                source_label=source_label,
                file_status=file_status,
                index={"prepare": prepare_receipt},
                read_back={
                    "file_absent": False,
                    "sql_source_absent": prepare_receipt.get("sql_source_absent"),
                    "sql_chunks_absent": prepare_receipt.get("sql_chunks_absent"),
                    "vector_points_remaining": prepare_receipt.get("vector_points_remaining"),
                },
                source_id=source_handle or "",
                errors=tuple(dict.fromkeys(errors)),
            )

        try:
            finalize_result = await self.memory_indexer.finalize_source_deletion(
                tenant_id=tenant_id,
                user_id=user_id,
                source_path=index_source_path or str(target),
                source_id=prepare_result.source_id,
                source_handle=source_handle,
                source_absent_verified=file_absent,
            )
            finalize_receipt = finalize_result.to_dict()
            errors.extend(finalize_result.errors)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.compat.runtime_adapter.internal_failure", exc
            )
            finalize_result = None
            finalize_receipt = {
                "status": "partial",
                "completed": False,
                "retryable": True,
                "errors": ["memory_index_finalize_failed"],
            }
            errors.append("memory_index_finalize_failed")

        completed = bool(finalize_result and finalize_result.completed and file_absent)
        if completed and source_handle:
            try:
                self.memory_store.clear_deletion_marker(
                    tenant_id,
                    user_id,
                    str(target),
                    source_handle=source_handle,
                    legacy_owner_proven=legacy_owner_proven,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                errors.append("memory_source_marker_cleanup_failed")
            marker_remaining = self.memory_store.deletion_marker_exists(
                tenant_id,
                user_id,
                str(target),
                source_handle=source_handle,
                legacy_owner_proven=legacy_owner_proven,
            )
            if marker_remaining:
                completed = False
                errors.append("memory_source_marker_cleanup_pending")
        read_back = {
            "file_absent": file_absent,
            "sql_source_absent": finalize_receipt.get("sql_source_absent"),
            "sql_chunks_absent": finalize_receipt.get("sql_chunks_absent"),
            "vector_points_remaining": prepare_receipt.get("vector_points_remaining"),
        }
        return MemoryDeleteResult(
            status="completed" if completed else "partial",
            completed=completed,
            retryable=not completed,
            source_found=file_existed or bool(prepare_receipt.get("source_found")),
            source_label=source_label,
            file_status=file_status,
            index={
                "prepare": prepare_receipt,
                "finalize": finalize_receipt,
            },
            read_back=read_back,
            source_id=source_handle or "",
            errors=tuple(dict.fromkeys(errors)),
        )

    async def inspect_memory_sources(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Return safe active and retryable-pending source handles."""

        inventory = self.memory_store.inspect_user_tree(tenant_id, user_id)
        sources = list(inventory.get("sources") or [])
        legacy_quarantined = 0
        scoped_records: list[dict[str, Any]] = []
        list_records = getattr(self.memory_indexer, "list_scoped_source_records", None)
        if list_records is not None:
            try:
                scoped_records = await list_records(
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                legacy_sources, legacy_quarantined = self.memory_store.inspect_legacy_records(
                    tenant_id,
                    user_id,
                    scoped_records,
                )
                sources.extend(
                    {key: value for key, value in source.items() if not key.startswith("_")}
                    for source in legacy_sources
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                legacy_quarantined += 1

        represented_paths: set[Path] = set()
        represented_index_paths: set[str] = set()
        for source in sources:
            source_id = str(source.get("source_id") or "")
            local_record = self.memory_store.resolve_source_handle_record(
                tenant_id,
                user_id,
                source_id,
            )
            if local_record is None and scoped_records:
                local_record = self.memory_store.resolve_legacy_source_handle_record(
                    tenant_id,
                    user_id,
                    source_id,
                    scoped_records,
                )
            if local_record is None:
                continue
            represented_paths.add(Path(str(local_record["_path"])))
            raw_index_path = str(local_record.get("_index_source_path") or "")
            if raw_index_path:
                represented_index_paths.add(raw_index_path)

        for record in scoped_records:
            raw_path = str(record.get("source_path") or "")
            source_handle = str(record.get("source_handle") or "")
            owner_proven = record.get("owner_proven") is True
            current_target = self.memory_store.resolve_owned_source(
                tenant_id,
                user_id,
                raw_path,
            )
            legacy_target = self.memory_store.resolve_legacy_owned_source(
                tenant_id,
                user_id,
                raw_path,
                owner_proven=owner_proven,
            )
            target = current_target or legacy_target
            if raw_path in represented_index_paths or (
                target is not None and target in represented_paths
            ):
                continue
            if not owner_proven:
                legacy_candidate = self.memory_store._legacy_candidate_from_record(
                    tenant_id,
                    user_id,
                    raw_path,
                )
                if legacy_candidate is None or not legacy_candidate.exists():
                    legacy_quarantined += 1
                continue
            if (
                not self.memory_store.is_source_handle(source_handle)
                or not str(record.get("source_id") or "")
                or target is None
            ):
                legacy_quarantined += 1
                continue
            if target.exists():
                # A present but unrepresented path is a symlink/unsafe or
                # ambiguous generation. Never downgrade it to SQL-only authority.
                legacy_quarantined += 1
                continue
            sources.append(
                {
                    "source_id": source_handle,
                    "label": public_source_label(target),
                    "source_type": str(record.get("source_type") or "unknown"),
                    "status": "active",
                    "derived_only": True,
                }
            )
        try:
            pending = await self.memory_indexer.list_pending_source_deletions(
                tenant_id=tenant_id,
                user_id=user_id,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.compat.runtime_adapter.internal_failure", exc
            )
            inventory["status"] = "partial"
            inventory["errors"] = ["memory_pending_deletion_inspect_failed"]
            return inventory

        by_handle = {str(item.get("source_id") or ""): item for item in sources}
        for item in pending:
            by_handle[str(item.get("source_id") or "")] = item
        merged = sorted(
            by_handle.values(),
            key=lambda item: (str(item.get("label") or ""), str(item.get("source_id") or "")),
        )
        inventory["sources"] = merged
        inventory["files"] = [str(item.get("label") or "") for item in merged]
        inventory["file_count"] = len(merged)
        inventory["source_types"] = sorted(
            {str(item.get("source_type") or "unknown") for item in merged}
        )
        inventory["status"] = "ok"
        inventory["legacy_quarantined_sources"] = legacy_quarantined + sum(
            1 for item in pending if item.get("status") == "ownership_quarantined"
        )
        return inventory

    async def delete_memory_source_by_id(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_id: str,
        expected_database_source_handle: str | None = None,
    ) -> MemoryDeleteResult:
        """Delete a source selected from the scoped inspection inventory."""

        if not self.memory_store.is_source_handle(source_id):
            return MemoryDeleteResult(
                status="rejected",
                completed=False,
                retryable=False,
                source_found=False,
                source_label="",
                file_status="out_of_scope",
                index={},
                read_back={"file_absent": None},
                source_id="",
                errors=("memory_source_not_found",),
            )

        def unresolved(
            error: str = "memory_source_deletion_state_unresolved",
        ) -> MemoryDeleteResult:
            return MemoryDeleteResult(
                status="unresolved",
                completed=False,
                retryable=True,
                source_found=False,
                source_label="",
                file_status="unknown",
                index={"status": "deletion_state_unresolved", "completed": False},
                read_back={
                    "file_absent": None,
                    "sql_source_absent": None,
                    "sql_chunks_absent": None,
                    "vector_points_remaining": None,
                },
                source_id=source_id,
                errors=(error,),
            )

        local_record = self.memory_store.resolve_source_handle_record(
            tenant_id,
            user_id,
            source_id,
        )
        if local_record is None:
            list_records = getattr(self.memory_indexer, "list_scoped_source_records", None)
            if list_records is not None:
                try:
                    scoped_records = await list_records(
                        tenant_id=tenant_id,
                        user_id=user_id,
                    )
                    local_record = self.memory_store.resolve_legacy_source_handle_record(
                        tenant_id,
                        user_id,
                        source_id,
                        scoped_records,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                        exc,
                    )

        if local_record is None:
            resolve_database_handle = getattr(
                self.memory_indexer,
                "resolve_scoped_source_handle",
                None,
            )
            if resolve_database_handle is not None:
                try:
                    database_record = await resolve_database_handle(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        source_handle=source_id,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                        exc,
                    )
                    database_record = None
                if database_record is not None and database_record.get("owner_proven") is True:
                    raw_path = str(database_record.get("source_path") or "")
                    current_target = self.memory_store.resolve_owned_source(
                        tenant_id,
                        user_id,
                        raw_path,
                    )
                    legacy_target = self.memory_store.resolve_legacy_owned_source(
                        tenant_id,
                        user_id,
                        raw_path,
                        owner_proven=True,
                    )
                    target = current_target or legacy_target
                    if target is not None:
                        local_record = {
                            "_path": str(target),
                            "_index_source_path": raw_path,
                            "legacy": current_target is None,
                            "database_only": True,
                        }

        needs_receipt_proof = local_record is None or (
            local_record.get("_deletion_stage") == "finalizing"
        )
        pending: dict[str, Any] | None = None
        completed_receipt: dict[str, Any] | None = None
        if needs_receipt_proof:
            try:
                pending = await self.memory_indexer.resolve_pending_source_handle(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_handle=source_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
            try:
                completed_receipt = await self.memory_indexer.resolve_completed_source_deletion(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_handle=source_id,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )

        def resolve_persisted_path(
            record: dict[str, Any],
        ) -> tuple[Any | None, bool]:
            if record.get("owner_proven") is not True:
                return None, False
            raw_path = str(record.get("source_path") or "")
            current = self.memory_store.resolve_owned_source(
                tenant_id,
                user_id,
                raw_path,
            )
            if current is not None:
                return current, False
            legacy = self.memory_store.resolve_legacy_owned_source(
                tenant_id,
                user_id,
                raw_path,
                owner_proven=True,
            )
            return legacy, legacy is not None

        if completed_receipt is not None:
            receipt_proven = (
                completed_receipt.get("sql_source_absent") is True
                and completed_receipt.get("sql_chunks_absent") is True
                and completed_receipt.get("vector_points_remaining") == 0
            )
            completed_path, completed_is_legacy = resolve_persisted_path(completed_receipt)
            if not receipt_proven or completed_path is None:
                return unresolved("memory_completed_receipt_unverified")
            if local_record is not None and Path(str(local_record["_path"])) != completed_path:
                return unresolved("memory_completed_receipt_path_mismatch")
            if completed_path.exists():
                return MemoryDeleteResult(
                    status="conflict",
                    completed=False,
                    retryable=False,
                    source_found=True,
                    source_label=public_source_label(completed_path),
                    file_status="new_generation_present",
                    index={"status": "generation_conflict", "completed": False},
                    read_back={
                        "file_absent": False,
                        "sql_source_absent": None,
                        "sql_chunks_absent": None,
                        "vector_points_remaining": None,
                    },
                    source_id=source_id,
                    errors=("memory_source_generation_conflict",),
                )
            try:
                self.memory_store.clear_deletion_marker(
                    tenant_id,
                    user_id,
                    str(completed_path),
                    source_handle=source_id,
                    legacy_owner_proven=completed_is_legacy,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                    exc,
                )
                return unresolved("memory_source_marker_cleanup_failed")
            if self.memory_store.deletion_marker_exists(
                tenant_id,
                user_id,
                str(completed_path),
                source_handle=source_id,
                legacy_owner_proven=completed_is_legacy,
            ):
                return unresolved("memory_source_marker_cleanup_pending")
            return MemoryDeleteResult(
                status="completed",
                completed=True,
                retryable=False,
                source_found=False,
                source_label=public_source_label(completed_path),
                file_status="absent",
                index={
                    "status": "idempotent_verified_absent",
                    "completed": True,
                    "receipt_persisted": True,
                },
                read_back={
                    "file_absent": True,
                    "sql_source_absent": True,
                    "sql_chunks_absent": True,
                    "vector_points_remaining": 0,
                },
                source_id=source_id,
            )

        index_source_path: str | None = None
        legacy_owner_proven = bool(local_record and local_record.get("legacy") is True)
        source_path = Path(str(local_record["_path"])) if local_record else None
        if local_record and legacy_owner_proven:
            index_source_path = str(local_record.get("_index_source_path") or "") or None

        if pending is not None:
            pending_path, pending_is_legacy = resolve_persisted_path(pending)
            if pending_path is None:
                return unresolved("memory_pending_source_owner_unproven")
            if source_path is not None and source_path != pending_path:
                return unresolved("memory_pending_source_path_mismatch")
            source_path = pending_path
            legacy_owner_proven = pending_is_legacy
            index_source_path = str(pending.get("source_path") or "")

        if local_record and local_record.get("_deletion_stage") == "finalizing" and not pending:
            # Content is already gone; creating a fresh SQL tombstone here
            # would turn an unproven local marker into a false completion.
            return unresolved("memory_finalizing_receipt_missing")
        if source_path is None:
            return unresolved()
        return await self.delete_memory_source(
            tenant_id=tenant_id,
            user_id=user_id,
            source_path=str(source_path),
            expected_source_handle=source_id,
            index_source_path=index_source_path,
            legacy_owner_proven=legacy_owner_proven,
            expected_database_source_handle=(
                expected_database_source_handle
                or (
                    source_id
                    if local_record is not None and local_record.get("database_only") is True
                    else None
                )
            ),
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
            background_sync = await self.flush_pending_memory_sync(timeout=5.0)
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
                "background_sync": background_sync,
                "hook": hook_result,
                "flush": flush_result,
            }
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.runtime.compat.runtime_adapter.internal_failure",
                exc,
            )
            return {
                "status": "failed",
                "flushed": False,
                "reason": "memory_pre_compact_flush_failed",
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
