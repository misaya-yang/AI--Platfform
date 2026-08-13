"""Session memory, history compaction, and streaming context lifecycle."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import uuid
from typing import TYPE_CHECKING, Any

from ai_gateway_core.enums import StreamEventType
from ai_gateway_core.logging import get_logger, record_internal_exception

from ..memory.compressor import (
    ContextCompressor,
    ModelRegistryLLMService,
)
from ..quality.cache_optimizer import (
    stable_cache_hash,
)
from ..rag.context_engine import (
    ContextBudgetManager,
    ContextStructure,
    _history_units,
)
from ..run_budget import (
    RunBudget,
    RunBudgetExceeded,
)
from ..runtime.context import (
    ContextAssemblerV2,
    ContextPacketIntegrityError,
)
from ..runtime.memory.lifecycle import (
    context_hash,
    memory_content_hash,
    memory_policy_enabled,
)
from ..runtime.memory.working_state import (
    bounded_working_memory_context,
    persist_working_memory,
    restore_working_memory,
)
from ..working_memory import WorkingMemory
from .agent_loop_helpers import (
    _redact_trace_text,
)
from .agent_loop_models import (
    AgentLoopContext,
    AgentLoopEvent,
    AgentLoopPhase,
    _env_enabled,
    _env_int,
)
from .runtime_context import compose_agent_system_prompt

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike


logger = get_logger(__name__)


class AgentContextLifecycleMixin:
    """Internal methods extracted from :class:`AgentLoop` without behavior changes."""

    @staticmethod
    def _agent_loop_compat():
        """Resolve public monkeypatch seams without coupling import initialization."""
        from . import agent_loop

        return agent_loop

    async def _persistent_session_owner_matches(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        """Prove legacy-memory ownership against the durable session record."""

        if self.session_manager is None:
            return False
        try:
            durable_session = await self.session_manager.get(session_id)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            return False
        if durable_session is None:
            return False
        if durable_session.tenant_id != tenant_id or durable_session.user_id != user_id:
            raise PermissionError("Durable session owner mismatch")
        return True

    async def _bind_session_working_memory(
        self,
        *,
        ctx: AgentLoopContext,
        session: Any,
    ) -> None:
        """Cold-restore one shared WorkingMemory under the session lock."""

        async with session.lock:
            live_session = await self.task_manager.get_session(
                ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
            if live_session is not session:
                raise RuntimeError("Session unavailable during run initialization")

            if session.working_memory is None:
                session.working_memory = WorkingMemory(session_id=ctx.session_id)

            hydrated = bool(getattr(session, "_assistant_working_memory_hydrated", False))
            if self.memory_service is not None and not hydrated:
                legacy_owner_verified = await self._persistent_session_owner_matches(
                    session_id=ctx.session_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                )
                session._assistant_working_memory_legacy_owner_verified = legacy_owner_verified
                try:
                    restored = await restore_working_memory(
                        self.memory_service,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        session_id=ctx.session_id,
                        legacy_owner_verified=legacy_owner_verified,
                    )
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.agent.agent_context_lifecycle.internal_failure",
                        exc,
                    )
                    ctx.working_memory_restore_failed = True
                else:
                    if restored is not None:
                        session.working_memory = restored
                    session._assistant_working_memory_hydrated = True
            elif self.memory_service is None:
                session._assistant_working_memory_hydrated = True

            ctx.working_memory_legacy_owner_verified = bool(
                getattr(
                    session,
                    "_assistant_working_memory_legacy_owner_verified",
                    False,
                )
            )
            ctx.working_memory = session.working_memory

    async def _persist_session_working_memory(
        self,
        *,
        ctx: AgentLoopContext,
        session: Any,
    ) -> bool:
        """Persist the current shared object only while its live owner lock is held."""

        if self.memory_service is None or ctx.working_memory_restore_failed:
            return False
        async with session.lock:
            live_session = await self.task_manager.get_session(
                ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
            )
            if live_session is not session or session.working_memory is None:
                logger.warning("Working memory persistence skipped for a deleted session")
                return False
            if ctx.working_memory is not session.working_memory:
                logger.warning("Working memory persistence skipped for a stale run snapshot")
                return False
            try:
                persisted = await persist_working_memory(
                    self.memory_service,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    memory=session.working_memory,
                    write_legacy_compat=ctx.working_memory_legacy_owner_verified,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )
                return False
            if persisted:
                logger.debug(
                    "Persisted working memory with %d tasks",
                    len(session.working_memory.tasks),
                )
            else:
                logger.warning("Working memory persistence was not confirmed")
            return persisted

    async def _preprocess_history(
        self,
        history: list[dict[str, Any]],
        max_tokens: int,
        min_recent: int,
        model_id: str | None = None,
        ctx: AgentLoopContext | None = None,
    ) -> list[dict[str, Any]]:
        """Prepare and validate a bounded child history, then return it for commit.

        The caller-owned parent list is never mutated. Successful replacement
        uses the same flush, protected-state, tool-pair, and lineage primitive
        as explicit ``context_compact``. Without an owner-bound run context the
        method fails closed and preserves the parent.
        """
        if not history:
            return history

        total_tokens = self._agent_loop_compat().estimate_history_tokens(history)
        normalized_max_tokens = max(1, int(max_tokens))
        normalized_min_recent = max(1, int(min_recent))

        def record_receipt(
            *,
            stats: dict[str, Any],
            status: str,
            pre_compaction_flush: dict[str, Any] | None = None,
            candidate_tokens: int | None = None,
        ) -> None:
            if ctx is None:
                return
            allowed_reasons = {
                "within_budget",
                "run_context_unavailable",
                "no_user_turn",
                "not_enough_turns",
                "nothing_to_compact",
                "unresolved_tool_state",
                "summary_unavailable",
                "summary_failed",
                "protected_plan_invalid",
                "protected_request_validation_failed",
                "protected_system_validation_failed",
                "protected_constraint_validation_failed",
                "protected_plan_validation_failed",
                "tool_pair_validation_failed",
                "no_token_reduction",
                "lineage_failed",
                "lineage_validation_failed",
                "pre_compaction_flush_failed",
                "compaction_prepare_failed",
                "compacted_child_exceeds_budget",
                "compacted",
            }
            raw_reason = str(
                stats.get("reason") or ("compacted" if stats.get("compacted") else "")
            ).strip()
            safe_reason = (
                raw_reason if raw_reason in allowed_reasons else "compaction_prepare_failed"
            )
            receipt: dict[str, Any] = {
                "schema_version": "assistant-history-compaction/v1",
                "trigger": "history_preprocess",
                "status": status,
                "compacted": bool(stats.get("compacted")) and status == "committed",
                "reason": safe_reason,
                "parent_context_hash": context_hash(history),
                "parent_preserved": status != "committed",
                "tokens_before": int(stats.get("tokens_before") or total_tokens),
                "tokens_after": (
                    int(stats.get("tokens_after") or total_tokens)
                    if status == "committed"
                    else total_tokens
                ),
                "max_tokens": normalized_max_tokens,
                "turns_total": int(stats.get("turns_total") or 0),
                "turns_kept": int(stats.get("turns_kept") or 0),
                "messages_summarized": int(stats.get("messages_summarized") or 0),
            }
            if candidate_tokens is not None:
                receipt["candidate_tokens"] = max(0, int(candidate_tokens))
            lineage = stats.get("compaction_lineage")
            if isinstance(lineage, dict):
                receipt["compaction_lineage"] = copy.deepcopy(lineage)
            if isinstance(pre_compaction_flush, dict):
                raw_flush_status = str(pre_compaction_flush.get("status") or "").lower()
                receipt["pre_compaction_flush"] = {
                    "status": raw_flush_status
                    if raw_flush_status in {"ok", "noop", "failed", "blocked"}
                    else "invalid",
                    "flushed": pre_compaction_flush.get("flushed") is True,
                }
            ctx.history_compaction_receipt = receipt

        if total_tokens <= normalized_max_tokens:
            turns_total = sum(1 for message in history if message.get("role") == "user")
            stats = self._compaction_noop_stats(
                history,
                reason="within_budget",
                turns_total=turns_total,
                turns_kept=turns_total,
            )
            record_receipt(stats=stats, status="not_needed")
            logger.debug(
                "History within budget: %d tokens (max: %d)",
                total_tokens,
                normalized_max_tokens,
            )
            return history

        logger.info(
            "History exceeds budget (%d > %d tokens); preparing lineage-backed compaction",
            total_tokens,
            normalized_max_tokens,
        )
        if ctx is None:
            return history

        user_indices = [
            index for index, message in enumerate(history) if message.get("role") == "user"
        ]
        if not user_indices:
            stats = self._compaction_noop_stats(
                history,
                reason="no_user_turn",
                turns_total=0,
                turns_kept=0,
            )
            record_receipt(stats=stats, status="preserved_parent")
            return history

        # Keep the smallest number of complete recent user turns whose suffix
        # contains at least the configured message floor. This protects the
        # full current turn instead of slicing a raw message suffix.
        keep_recent_turns = len(user_indices)
        for turns in range(1, len(user_indices) + 1):
            if len(history) - user_indices[-turns] >= normalized_min_recent:
                keep_recent_turns = turns
                break

        candidate = copy.deepcopy(history)
        stats, pre_compaction_flush = await self._compact_messages_after_flush(
            ctx=ctx,
            messages=candidate,
            keep_recent_turns=keep_recent_turns,
            reason="history_preprocess",
            model_id=model_id,
        )
        if not stats.get("compacted"):
            record_receipt(
                stats=stats,
                status="preserved_parent",
                pre_compaction_flush=pre_compaction_flush,
            )
            return history

        candidate_tokens = self._agent_loop_compat().estimate_history_tokens(candidate)
        if candidate_tokens > normalized_max_tokens:
            rejected_stats = dict(stats)
            rejected_stats["compacted"] = False
            rejected_stats["reason"] = "compacted_child_exceeds_budget"
            record_receipt(
                stats=rejected_stats,
                status="preserved_parent",
                pre_compaction_flush=pre_compaction_flush,
                candidate_tokens=candidate_tokens,
            )
            return history

        record_receipt(
            stats=stats,
            status="committed",
            pre_compaction_flush=pre_compaction_flush,
            candidate_tokens=candidate_tokens,
        )
        return candidate

    @staticmethod
    def _compaction_noop_stats(
        messages: list[dict[str, Any]],
        *,
        reason: str,
        turns_total: int,
        turns_kept: int,
    ) -> dict[str, Any]:
        """Describe a failed/no-op compaction without touching the parent list."""

        from . import agent_loop

        tokens = agent_loop.estimate_history_tokens(messages)
        return {
            "compacted": False,
            "reason": reason,
            "turns_total": turns_total,
            "turns_kept": turns_kept,
            "tokens_before": tokens,
            "tokens_after": tokens,
        }

    @staticmethod
    def _compaction_message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
        return str(content or "").strip()

    @classmethod
    def _protected_compaction_constraints(
        cls,
        messages: list[dict[str, Any]],
    ) -> list[str]:
        """Extract prior hard constraints that a generated summary may omit."""

        markers = (
            "hard constraint",
            "must ",
            "must not",
            "never ",
            "do not",
            "don't ",
            "should not",
            "may not",
            "cannot",
            "can't ",
            "only ",
            "without ",
            "keep ",
            "preserve ",
            "required",
            "requirement",
            "acceptance criteria",
            "constraint",
            "硬约束",
            "必须",
            "不得",
            "禁止",
            "不能",
            "不要",
            "不可",
            "只能",
            "仅限",
            "保留",
            "保持",
            "验收标准",
        )
        protected: list[str] = []
        seen: set[str] = set()
        for message in messages:
            metadata = message.get("metadata")
            explicit = any(
                bool(message.get(key)) for key in ("protected", "hard_constraint", "constraint")
            ) or (
                isinstance(metadata, dict)
                and any(
                    bool(metadata.get(key))
                    for key in ("protected", "hard_constraint", "constraint")
                )
            )
            if message.get("role") not in {"user", "system", "developer"} and not explicit:
                continue
            text = cls._compaction_message_text(message)
            if not text:
                continue
            normalized = text.casefold()
            if explicit or any(marker in normalized for marker in markers):
                safe_text = _redact_trace_text(text)
                if safe_text not in seen:
                    seen.add(safe_text)
                    protected.append(safe_text)
        return protected

    @staticmethod
    def _valid_compaction_lineage(
        lineage: Any,
        *,
        parent_messages: list[dict[str, Any]],
        child_messages: list[dict[str, Any]],
        summary_text: str,
    ) -> bool:
        if not isinstance(lineage, dict):
            return False
        provenance = lineage.get("summary_provenance")
        return bool(
            lineage.get("compaction_id")
            and lineage.get("parent_context_hash") == context_hash(parent_messages)
            and lineage.get("child_context_hash") == context_hash(child_messages)
            and lineage.get("summary_hash") == memory_content_hash(summary_text)[:16]
            and isinstance(provenance, dict)
            and provenance.get("untrusted") is True
        )

    async def _compact_messages_by_turns(
        self,
        messages: list[dict[str, Any]],
        keep_recent_turns: int,
        model_id: str,
        *,
        use_llm_summary: bool = True,
        protected_plan: dict[str, Any] | None = None,
        reason: str = "context_compact",
        run_budget: RunBudget | None = None,
        staged_compaction_enabled: bool | None = None,
        staged_compaction_min_source_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Prepare, validate, then atomically commit a turn-based compaction.

        The parent list remains byte-for-byte and object-order unchanged until
        summary generation, protected-field checks, token reduction, tool-pair
        validation, and lineage construction all succeed.
        """

        normalized_keep_turns = max(1, int(keep_recent_turns))
        user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
        turns_total = len(user_indices)
        turns_kept = min(turns_total, normalized_keep_turns)
        if turns_total <= normalized_keep_turns:
            return self._compaction_noop_stats(
                messages,
                reason="not_enough_turns",
                turns_total=turns_total,
                turns_kept=turns_total,
            )

        cutoff_idx = user_indices[-normalized_keep_turns]
        head_system: list[dict[str, Any]] = []
        first_non_system = 0
        for index, message in enumerate(messages):
            if message.get("role") != "system":
                break
            head_system.append(message)
            first_non_system = index + 1

        old_messages = messages[first_non_system:cutoff_idx]
        recent_messages = messages[cutoff_idx:]
        if not old_messages:
            return self._compaction_noop_stats(
                messages,
                reason="nothing_to_compact",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        # An incomplete historical tool exchange is executable state, not
        # summarizable prose. Keep the parent intact and let the caller resume
        # or resolve it explicitly.
        _, invalid_old_tool_messages = _history_units(old_messages)
        if invalid_old_tool_messages:
            return self._compaction_noop_stats(
                messages,
                reason="unresolved_tool_state",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        parent_messages = copy.deepcopy(messages)
        current_request = copy.deepcopy(messages[user_indices[-1]])
        before_tokens = self._agent_loop_compat().estimate_history_tokens(parent_messages)

        if not self.model_registry or not use_llm_summary:
            return self._compaction_noop_stats(
                messages,
                reason="summary_unavailable",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        try:
            compressor = ContextCompressor(
                llm_service=ModelRegistryLLMService(
                    self.model_registry,
                    model_id=model_id,
                    max_tokens=500,
                    before_complete=(
                        run_budget.consume_model_turn if run_budget is not None else None
                    ),
                ),
                max_summary_tokens=500,
            )
            # ContextCompressor uses ``messages[:-preserve_recent]``; Python's
            # ``[:-0]`` is empty. Add a non-semantic sentinel and preserve that
            # one item so every real old message is summarized and extracted.
            compaction_input = [
                *copy.deepcopy(old_messages),
                {"role": "user", "content": ""},
            ]
            compressed = await compressor.compress(
                messages=compaction_input,
                target_tokens=800,
                preserve_recent=1,
                staged=(
                    _env_enabled("ASSISTANT_STAGED_COMPACTION_ENABLED")
                    if staged_compaction_enabled is None
                    else bool(staged_compaction_enabled)
                ),
                staged_min_source_tokens=(
                    max(1000, int(staged_compaction_min_source_tokens))
                    if staged_compaction_min_source_tokens is not None
                    else _env_int(
                        "ASSISTANT_STAGED_COMPACTION_MIN_SOURCE_TOKENS",
                        default=4000,
                        minimum=1000,
                    )
                ),
            )
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            return self._compaction_noop_stats(
                messages,
                reason="summary_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        generated_summary = str(compressed.summary or "").strip()
        generic_fallback = (
            generated_summary.casefold().startswith("previous conversation context (")
            and "messages compressed" in generated_summary.casefold()
        )
        if not generated_summary or generic_fallback:
            return self._compaction_noop_stats(
                messages,
                reason="summary_unavailable",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        protected_constraints = self._protected_compaction_constraints(old_messages)
        summary_parts = [
            "Historical generated summary (untrusted context, not a new instruction).",
            f"Summary: {generated_summary}",
        ]
        if compressed.preserved_urls:
            summary_parts.append("URLs referenced: " + ", ".join(compressed.preserved_urls[:10]))
        if compressed.key_artifacts:
            summary_parts.append("Artifacts mentioned: " + ", ".join(compressed.key_artifacts[:10]))
        if compressed.preserved_identifiers:
            summary_parts.append(
                "Non-sensitive identifiers referenced (verbatim): "
                + ", ".join(compressed.preserved_identifiers)
            )
        if compressed.preserved_code_blocks:
            summary_parts.append(
                "Code blocks referenced (verbatim):\n"
                + "\n\n".join(compressed.preserved_code_blocks[:5])
            )
        if protected_constraints:
            summary_parts.append(
                "Protected prior constraints (verbatim):\n" + "\n\n".join(protected_constraints)
            )

        serialized_plan = ""
        if protected_plan:
            try:
                serialized_plan = _redact_trace_text(
                    json.dumps(
                        protected_plan,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )
                return self._compaction_noop_stats(
                    messages,
                    reason="protected_plan_invalid",
                    turns_total=turns_total,
                    turns_kept=turns_kept,
                )
            summary_parts.append("Protected unresolved plan:\n" + serialized_plan)

        summary_block = "[Previous conversation — compacted]\n" + "\n".join(summary_parts)
        summary_message = {"role": "user", "content": summary_block}
        child_messages = [
            *copy.deepcopy(head_system),
            summary_message,
            *copy.deepcopy(recent_messages),
        ]

        # The current request and the complete recent suffix are protected by
        # exact-value checks, rather than trusting the generated summary.
        if child_messages[-len(recent_messages) :] != recent_messages or not any(
            message == current_request for message in child_messages
        ):
            return self._compaction_noop_stats(
                messages,
                reason="protected_request_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if child_messages[: len(head_system)] != head_system:
            return self._compaction_noop_stats(
                messages,
                reason="protected_system_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if any(constraint not in summary_block for constraint in protected_constraints):
            return self._compaction_noop_stats(
                messages,
                reason="protected_constraint_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if serialized_plan and serialized_plan not in summary_block:
            return self._compaction_noop_stats(
                messages,
                reason="protected_plan_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        _, invalid_child_tool_messages = _history_units(child_messages[len(head_system) :])
        if invalid_child_tool_messages:
            return self._compaction_noop_stats(
                messages,
                reason="tool_pair_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        after_tokens = self._agent_loop_compat().estimate_history_tokens(child_messages)
        if after_tokens >= before_tokens:
            return self._compaction_noop_stats(
                messages,
                reason="no_token_reduction",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        minimum_savings = max(1, (before_tokens + 9) // 10)
        if before_tokens - after_tokens < minimum_savings:
            return self._compaction_noop_stats(
                messages,
                reason="insufficient_token_savings",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        try:
            compaction_lineage = self._agent_loop_compat().build_compaction_lineage(
                parent_messages=parent_messages,
                child_messages=child_messages,
                summary_text=summary_block,
                reason=reason,
                turns_total=turns_total,
                turns_kept=turns_kept,
                messages_summarized=len(old_messages),
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            return self._compaction_noop_stats(
                messages,
                reason="lineage_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        if not self._valid_compaction_lineage(
            compaction_lineage,
            parent_messages=parent_messages,
            child_messages=child_messages,
            summary_text=summary_block,
        ):
            return self._compaction_noop_stats(
                messages,
                reason="lineage_validation_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )

        # Commit is deliberately the first and only mutation of the live list.
        messages[:] = child_messages
        logger.info(
            "context_compact: %d → %d tokens (kept %d turns, summarized %d msgs)",
            before_tokens,
            after_tokens,
            turns_kept,
            len(old_messages),
        )
        return {
            "compacted": True,
            "turns_total": turns_total,
            "turns_kept": turns_kept,
            "messages_summarized": len(old_messages),
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
            "protected_constraints": len(protected_constraints),
            "protected_plan": bool(serialized_plan),
            "summary_stages": compressed.summary_stages,
            "minimum_savings_ratio": 0.1,
            "compaction_lineage": compaction_lineage,
            "loss": {
                "messages_replaced": len(old_messages),
                "generated_summary": True,
                "recent_suffix_preserved": True,
            },
        }

    async def _compact_messages_after_flush(
        self,
        *,
        ctx: AgentLoopContext,
        messages: list[dict[str, Any]],
        keep_recent_turns: int,
        reason: str,
        model_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Run the provider flush gate before preparing a child context."""

        normalized_keep_turns = max(1, int(keep_recent_turns))
        turns_total = sum(1 for message in messages if message.get("role") == "user")
        turns_kept = min(turns_total, normalized_keep_turns)
        pre_compaction_flush: dict[str, Any] | None = None
        agent_runtime = ctx.config.agent_runtime
        user_memory_enabled = memory_policy_enabled(
            memory_mode=getattr(ctx.config, "memory_mode", None),
            memory_profile=getattr(ctx.config, "memory_profile", None),
        )
        run_budget = getattr(ctx, "run_budget", None)
        if isinstance(ctx, AgentLoopContext) and run_budget is None:
            # Real runs always carry the canonical budget. Structural legacy
            # callers may omit it, but production compaction must not silently
            # escape model-turn accounting.
            raise RuntimeError("run_budget_not_initialized")
        if (
            self.assistant_runtime is not None
            and user_memory_enabled
            and (agent_runtime is None or agent_runtime.user_memory_enabled)
        ):
            try:
                pre_compaction_flush = await self.assistant_runtime.on_pre_compact(
                    tenant_id=ctx.tenant_id,
                    user_id=(
                        agent_runtime.memory_principal if agent_runtime is not None else ctx.user_id
                    ),
                    session_id=ctx.session_id,
                    run_id=ctx.run_id,
                    reason=reason,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )
                pre_compaction_flush = {
                    "status": "failed",
                    "flushed": False,
                    "reason": "pre_compaction_flush_error",
                }

            flush_status = (
                str(pre_compaction_flush.get("status") or "").strip().lower()
                if isinstance(pre_compaction_flush, dict)
                else "invalid"
            )
            nested_flush_failed = bool(
                isinstance(pre_compaction_flush, dict)
                and any(
                    isinstance(pre_compaction_flush.get(key), dict)
                    and str(pre_compaction_flush[key].get("status") or "").strip().lower()
                    in {"failed", "error", "blocked"}
                    for key in ("hook", "flush")
                )
            )
            hook_receipt = (
                pre_compaction_flush.get("hook")
                if isinstance(pre_compaction_flush, dict)
                and isinstance(pre_compaction_flush.get("hook"), dict)
                else pre_compaction_flush
            )
            flush_receipt = (
                pre_compaction_flush.get("flush")
                if isinstance(pre_compaction_flush, dict)
                and isinstance(pre_compaction_flush.get("flush"), dict)
                else pre_compaction_flush
            )
            flush_required = bool(
                isinstance(hook_receipt, dict) and hook_receipt.get("flush_required") is True
            )
            required_flush_missing = bool(
                flush_required
                and not (isinstance(flush_receipt, dict) and flush_receipt.get("flushed") is True)
            )
            if flush_status != "ok" or nested_flush_failed or required_flush_missing:
                if not isinstance(pre_compaction_flush, dict):
                    pre_compaction_flush = {
                        "status": "failed",
                        "flushed": False,
                        "reason": "pre_compaction_flush_invalid",
                    }
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="pre_compaction_flush_failed",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        protected_plan: dict[str, Any] = {}
        execution_plan = getattr(ctx, "execution_plan", None)
        if execution_plan is not None:
            try:
                protected_plan["execution_plan"] = execution_plan.to_dict()
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="protected_plan_invalid",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        working_memory = getattr(ctx, "working_memory", None)
        if working_memory is not None:
            try:
                working_snapshot = working_memory.to_dict()
                if not isinstance(working_snapshot, dict):
                    raise ValueError("working memory snapshot must be an object")
                raw_tasks = working_snapshot.get("tasks", [])
                if not isinstance(raw_tasks, list) or any(
                    not isinstance(task, dict) for task in raw_tasks
                ):
                    raise ValueError("working memory tasks must be objects")
                unresolved_statuses = {"pending", "in_progress", "blocked", "failed"}
                unresolved_tasks = [
                    copy.deepcopy(task)
                    for task in raw_tasks
                    if str(task.get("status") or "").strip().lower() in unresolved_statuses
                ]
                goal = working_snapshot.get("goal")
                if goal is not None or unresolved_tasks:
                    protected_plan["working_memory"] = {
                        "session_id": working_snapshot.get("session_id"),
                        "goal": copy.deepcopy(goal),
                        "tasks": unresolved_tasks,
                    }
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )
                return (
                    self._compaction_noop_stats(
                        messages,
                        reason="protected_plan_invalid",
                        turns_total=turns_total,
                        turns_kept=turns_kept,
                    ),
                    pre_compaction_flush,
                )

        try:
            stats = await self._compact_messages_by_turns(
                messages=messages,
                keep_recent_turns=normalized_keep_turns,
                model_id=model_id or ctx.config.model_id,
                # Explicit compaction always requires a real summary. The
                # Context Engine flag controls assembly, not whether history
                # may be replaced by a generic omission marker.
                use_llm_summary=True,
                protected_plan=protected_plan or None,
                reason=reason,
                run_budget=run_budget,
                staged_compaction_enabled=bool(
                    getattr(ctx.config, "enable_staged_compaction", False)
                ),
                staged_compaction_min_source_tokens=(
                    getattr(ctx.config, "staged_compaction_min_source_tokens", 4000)
                ),
            )
        except RunBudgetExceeded:
            raise
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            stats = self._compaction_noop_stats(
                messages,
                reason="compaction_prepare_failed",
                turns_total=turns_total,
                turns_kept=turns_kept,
            )
        return stats, pre_compaction_flush

    async def _summarize_history(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 500,
    ) -> str | None:
        """
        Summarize a list of messages into a concise summary.

        Uses the configured LLM to generate a summary of the conversation.

        Args:
            messages: Messages to summarize
            max_tokens: Maximum tokens for the summary

        Returns:
            Summary string or None if summarization fails
        """
        if not messages or not self.model_registry:
            return None

        # Build text representation of messages
        text_parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            text_parts.append(f"{role}: {content[:500]}")  # Truncate long messages

        conversation_text = "\n".join(text_parts)

        # Use a fast model for summarization
        try:
            from ..prompts import build_summary_prompt

            prompt = build_summary_prompt(
                content=conversation_text,
                summary_type="bullet",
                target_length=f"{max_tokens} tokens or less",
                focus_areas=["Key decisions", "Important context", "Action items"],
            )

            model = self.model_registry.get_model_for_task("summarization")
            if not model:
                model = self.model_registry.get_default_model()

            if not model:
                return None

            response = await model.generate(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.3,  # Lower temperature for factual summary
            )

            return response.content if response else None

        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            return None

    async def _persist_context_detail(
        self,
        ctx: AgentLoopContext,
        detail: dict[str, Any],
    ) -> None:
        """Persist context-detail metrics for observability when DB is available."""
        if not self.database:
            return

        try:
            await self.database.execute(
                """
                INSERT INTO assistant_context_breakdown (
                    breakdown_id, request_id, run_id, tenant_id, user_id, session_id,
                    model_id, total_tokens, total_chars, tokens_by_category,
                    top_contributors, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6,
                    $7, $8, $9, $10, $11, NOW()
                )
                """,
                str(uuid.uuid4()),
                ctx.request_id,
                ctx.run_id,
                ctx.tenant_id,
                ctx.user_id,
                ctx.session_id,
                ctx.config.model_id,
                int(detail.get("total_tokens") or 0),
                int(detail.get("total_chars") or 0),
                json.dumps(detail.get("tokens_by_category") or {}),
                json.dumps((detail.get("contributors") or [])[:20]),
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            try:
                await self.database.execute(
                    """
                    INSERT INTO assistant_context_breakdown (
                        request_id, run_id, tenant_id, user_id, session_id,
                        model_id, total_tokens, detail, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5,
                        $6, $7, $8, NOW()
                    )
                    """,
                    ctx.request_id,
                    ctx.run_id,
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.session_id,
                    ctx.config.model_id,
                    int(detail.get("total_tokens") or 0),
                    json.dumps(detail),
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.agent.agent_context_lifecycle.internal_failure",
                    exc,
                )

    async def _persist_streaming_user_message(
        self,
        ctx: AgentLoopContext,
        metadata: dict[str, Any],
    ) -> None:
        try:
            await self.session_manager.add_message(
                session_id=ctx.session_id,
                role="user",
                content=ctx.message,
                metadata=metadata,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )

    def _on_user_message_persist_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        task_error = None if task.cancelled() else task.exception()
        if task_error is not None:
            record_internal_exception(
                __name__,
                "assistant.agent.user_message.background_persistence_failed",
                task_error,
            )

    def _schedule_streaming_user_message_persistence(self, ctx: AgentLoopContext) -> None:
        if not ctx.config.persist_messages or not self.session_manager:
            return
        try:
            from datetime import datetime

            metadata: dict[str, Any] = {"timestamp": datetime.utcnow().isoformat()}
            if ctx.config.file_paths:
                image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
                metadata["attachments"] = [
                    {
                        "type": "image" if str(path).lower().endswith(image_exts) else "file",
                        "url": path,
                        "filename": str(path).split("/")[-1] if "/" in str(path) else str(path),
                    }
                    for path in ctx.config.file_paths
                ]
            task = asyncio.create_task(self._persist_streaming_user_message(ctx, metadata))
            self._background_tasks.add(task)
            task.add_done_callback(self._on_user_message_persist_done)
        except (RuntimeError, TypeError) as exc:
            record_internal_exception(
                __name__,
                "assistant.user_message.persistence_schedule_failed",
                exc,
            )

    async def _get_streaming_tools(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> tuple[list[dict[str, Any]], list[str], str]:
        if not self.tool_invoker:
            return [], [], stable_cache_hash([])

        invocation_context = self._build_invocation_context(ctx, user=user)
        tool_defs = await self.tool_invoker.get_tool_definitions_filtered(
            context=invocation_context,
        )
        ctx.tool_policy_snapshot = invocation_context.policy_snapshot
        try:
            from ..tools.connector_registry import get_connector_registry
            from ..tools.tool_registry import ToolCallRequest

            registry = get_connector_registry()
            claimed = registry.connector_tool_names()
            if claimed and invocation_context.capability_allowlist is None:
                connector_request = ToolCallRequest(
                    call_id=ctx.request_id if hasattr(ctx, "request_id") else "agent-tool-list",
                    tool_name="__connector_visibility_probe__",
                    arguments={},
                    user=user or ctx.user,
                    metadata={
                        "tenant_id": invocation_context.tenant_id,
                        "session_id": invocation_context.session_id,
                    },
                )
                visible = await registry.visible_tools(connector_request)
                tool_defs = [tool for tool in tool_defs if tool.name not in claimed]
                seen = {tool.name for tool in tool_defs}
                for connector_tool in visible:
                    if connector_tool.name not in seen:
                        tool_defs.append(connector_tool)
                        seen.add(connector_tool.name)
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )

        # ConnectorRegistry is a secondary catalog source. Re-run both the
        # immutable ceiling and the live policy check after merging; a revoke
        # or policy outage between the canonical list and this merge must hide
        # the connector from the model-facing catalog as well as invocation.
        authorization_filter = getattr(
            self.tool_invoker,
            "filter_tool_definitions_authorized",
            None,
        )
        if callable(authorization_filter):
            tool_defs = await authorization_filter(invocation_context, tool_defs)
        elif invocation_context.capability_allowlist is not None:
            # Preserve duck-typed/custom ToolInvoker compatibility. Built-in
            # RegistryToolInvoker supplies the fresh policy recheck above;
            # legacy fakes/adapters retain their existing allowlist contract.
            tool_defs = invocation_context.capability_allowlist.filter_definitions(tool_defs)
        kb_mode = str(ctx.config.kb_mode or "auto").strip().lower()
        if kb_mode in {"off", "disabled", "false", "0"}:
            tool_defs = [tool for tool in tool_defs if tool.name != "search_knowledge_base"]
        elif ctx.config.agent_runtime is not None:
            tool_mode_enabled = any(
                isinstance(dataset_config, dict) and dataset_config.get("mode") == "tool"
                for dataset_config in (ctx.config.kb_retrieval_configs or {}).values()
            )
            if not tool_mode_enabled:
                # Auto-bound Knowledge is retrieved before the model turn. The
                # internal KB tool remains callable by that scheduler but is not
                # exposed as a model-selected capability unless a Dataset is
                # explicitly configured for tool mode.
                tool_defs = [tool for tool in tool_defs if tool.name != "search_knowledge_base"]

        def _tool_schema(tool: Any) -> dict[str, Any]:
            try:
                return tool.to_openai_schema(compact=True)
            except TypeError:
                return tool.to_openai_schema()

        available_tool_schema_hash = stable_cache_hash(
            [_tool_schema(tool) for tool in sorted(tool_defs, key=lambda item: item.name)]
        )
        extra_always: set[str] = set()
        allowlist = ctx.config.capability_allowlist
        if allowlist is not None:
            extra_always.update(str(name) for name in getattr(allowlist, "tool_names", ()))
        if ctx.config.skills_enabled is not False:
            extra_always.update(
                tool.name for tool in tool_defs if str(tool.name).startswith("skill_")
            )
        selector = self._agent_loop_compat().select_tools
        selector_options: dict[str, Any] = {
            "mode": "budget" if str(ctx.config.execution_profile) == "power" else "discover",
            "extra_always": extra_always,
        }
        try:
            selector_parameters = inspect.signature(selector).parameters.values()
        except (TypeError, ValueError):
            selector_options = {}
        else:
            accepts_extra_options = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in selector_parameters
            )
            if not accepts_extra_options:
                supported_options = {
                    parameter.name
                    for parameter in selector_parameters
                    if parameter.kind
                    in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
                }
                selector_options = {
                    name: value
                    for name, value in selector_options.items()
                    if name in supported_options
                }
        selected = selector(tool_defs, ctx.message, **selector_options)
        tools: list[dict[str, Any]] = []
        for tool in selected:
            tools.append(_tool_schema(tool))
        names = [tool.name for tool in selected]
        logger.info(
            "[STREAMING-FIRST] All tools available: %s (web_search_preference=%s, kb_ids=%s)",
            names,
            ctx.config.web_search_enabled,
            ctx.config.kb_dataset_ids,
        )
        return tools, names, available_tool_schema_hash

    async def _prepare_streaming_skills(
        self,
        ctx: AgentLoopContext,
    ) -> tuple[list[AgentLoopEvent], bool]:
        """Load and bridge Skills into an isolated, per-run tool overlay."""

        events: list[AgentLoopEvent] = []
        ctx.runtime_skills_metadata = []
        ctx.runtime_skill_registry = None
        ctx.runtime_tool_registry = None
        exact_versions = ctx.config.allowed_skill_versions or {}

        def unavailable() -> AgentLoopEvent:
            return AgentLoopEvent(
                phase=AgentLoopPhase.GENERATION_STORAGE,
                event_type=StreamEventType.RUN_ERROR.value,
                data={
                    "run_id": ctx.run_id,
                    "thread_id": ctx.session_id,
                    "session_id": ctx.session_id,
                    "error": "AGENT_SKILL_UNAVAILABLE",
                },
            )

        if self.assistant_runtime is None:
            if exact_versions:
                return [unavailable()], False
            return events, True

        should_use_skills = (
            bool(ctx.config.skills_enabled)
            if ctx.config.skills_enabled is not None
            else bool(self.assistant_runtime.features.skills)
        )
        if not should_use_skills:
            if exact_versions:
                return [unavailable()], False
            return events, True

        from ..skills.tool_bridge import SkillToolBridge, skill_tool_name
        from ..tools.tool_registry import ToolRegistry

        runtime_skills = self.assistant_runtime.skill_registry.fork_runtime_view()
        runtime_tools = ToolRegistry()
        skill_scope = (ctx.tenant_id, ctx.user_id)
        try:
            if exact_versions:
                loaded = await runtime_skills.load_versions_from_database(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    allowed_versions=exact_versions,
                )
                if loaded != len(exact_versions):
                    raise RuntimeError("Exact Agent Skill count mismatch")
            else:
                loaded = await runtime_skills.load_from_database(
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    allowed_names=ctx.config.allowed_skill_ids,
                )
        except Exception as exc:  # noqa: BLE001 - exact Agent Skills fail closed
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            if exact_versions:
                return [unavailable()], False
            loaded = 0

        if loaded > 0:
            events.append(
                AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type="skill_loaded",
                    data={"loaded_count": loaded},
                )
            )

        bridge = SkillToolBridge(runtime_skills, runtime_tools)
        bridged = bridge.sync_all_skills(
            allowed_names=ctx.config.allowed_skill_ids,
            scope=skill_scope,
            allowed_versions=ctx.config.allowed_skill_versions,
        )
        if exact_versions and bridged != len(exact_versions):
            logger.warning(
                "Exact Agent Skill bridge count mismatch: expected=%s actual=%s",
                len(exact_versions),
                bridged,
            )
            return [unavailable()], False
        if exact_versions:
            expected_tools = {
                skill_tool_name(name, version_id) for name, version_id in exact_versions.items()
            }
            visible_tools = {
                definition.name for definition in runtime_tools.list_tools(user=ctx.user)
            }
            if not expected_tools.issubset(visible_tools):
                logger.warning("Exact Agent Skill is not authorized for the caller")
                return [unavailable()], False

        selected_skills = runtime_skills.select_for_query(
            ctx.message,
            # Keep the relevance order, then let the provider-aware context
            # budget choose how many instruction bodies fit.  A fixed first-N
            # cutoff made equally relevant installed skills unreachable.
            max_skills=None,
            allowed_names=ctx.config.allowed_skill_ids,
            scope=skill_scope,
            allowed_versions=ctx.config.allowed_skill_versions,
        )
        max_candidates = self.skill_candidate_hard_limit or _env_int(
            "ASSISTANT_SKILL_CANDIDATE_HARD_LIMIT",
            default=64,
            minimum=1,
        )
        if len(selected_skills) > max_candidates:
            events.append(
                AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type="skill_selection_bounded",
                    data={
                        "candidate_count": len(selected_skills),
                        "operator_limit": max_candidates,
                        "deferred_count": len(selected_skills) - max_candidates,
                    },
                )
            )
            selected_skills = selected_skills[:max_candidates]
        if selected_skills:
            ctx.runtime_skills_metadata = [
                selection.skill.to_dict() for selection in selected_skills
            ]
            events.append(
                AgentLoopEvent(
                    phase=AgentLoopPhase.GENERATION_STORAGE,
                    event_type="skill_selected",
                    data={
                        "skills": [
                            {
                                "name": selection.skill.name,
                                "version": selection.skill.version,
                                "score": selection.score,
                            }
                            for selection in selected_skills
                        ]
                    },
                )
            )

        ctx.runtime_skill_registry = runtime_skills
        ctx.runtime_tool_registry = runtime_tools
        return events, True

    async def _get_streaming_dataset_context(
        self,
        ctx: AgentLoopContext,
        user: UserContextLike,
    ) -> tuple[dict[str, str] | None, str]:
        dataset_ids = sorted(str(item) for item in (ctx.config.kb_dataset_ids or []))
        configured_retrieval = getattr(ctx.config, "kb_retrieval_configs", {}) or {}
        if not isinstance(configured_retrieval, dict):
            configured_retrieval = {}
        if not self.kb_service or not ctx.config.kb_dataset_ids:
            revision_hash = stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable" if dataset_ids else "empty"}
            )
            ctx.knowledge_provenance = {
                "state": "unavailable" if dataset_ids else "no_binding",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
            }
            return None, revision_hash
        try:
            rows = await asyncio.wait_for(self.kb_service.list_datasets(user), timeout=0.3)
            if not isinstance(rows, list):
                rows = []
            configured = set(dataset_ids)
            names = {
                str(row["dataset_id"]): str(row["name"])
                for row in rows
                if row and str(row.get("dataset_id") or "") in configured and row.get("name")
            }
            revision_rows = []
            for row in rows:
                if not isinstance(row, dict) or str(row.get("dataset_id") or "") not in configured:
                    continue
                revision_fingerprint = str(row.get("revision_fingerprint") or "")
                if (
                    not revision_fingerprint.startswith("sha256:")
                    or len(revision_fingerprint) != 71
                    or any(
                        char not in "0123456789abcdef"
                        for char in revision_fingerprint.removeprefix("sha256:")
                    )
                ):
                    continue
                revision_rows.append(
                    {
                        "dataset_id": str(row.get("dataset_id") or ""),
                        "revision_fingerprint": revision_fingerprint,
                        "retrieval_config": dict(
                            configured_retrieval.get(
                                str(row.get("dataset_id") or ""),
                                {},
                            )
                        ),
                    }
                )
            revision_rows.sort(key=lambda item: item["dataset_id"])
            catalog_complete = {item["dataset_id"] for item in revision_rows} == configured
            revision_hash = stable_cache_hash(
                {
                    "dataset_ids": dataset_ids,
                    "catalog_complete": catalog_complete,
                    "datasets": revision_rows,
                }
            )
            ctx.knowledge_provenance = {
                "state": "available" if catalog_complete else "unavailable",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
                "catalog_complete": catalog_complete,
            }
            return names or None, revision_hash
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.agent.agent_context_lifecycle.internal_failure", exc
            )
            revision_hash = stable_cache_hash(
                {"dataset_ids": dataset_ids, "catalog": "unavailable"}
            )
            ctx.knowledge_provenance = {
                "state": "unavailable",
                "dataset_ids": dataset_ids,
                "revision_hash": revision_hash,
                "content_mode": "live_latest",
                "historical_replayable": False,
            }
            return None, revision_hash

    @staticmethod
    def _build_streaming_system_prompt(
        ctx: AgentLoopContext,
        *,
        available_tool_names: list[str],
        dataset_name_map: dict[str, str] | None,
        capabilities_enabled: bool = True,
    ) -> tuple[str, str]:
        """Compile the trusted stable prompt from the exact effective capabilities."""

        from ..prompts.system_prompt_v2 import (
            ensure_external_content_boundary,
            get_streaming_first_prompt,
        )

        base_prompt = get_streaming_first_prompt(
            available_datasets=ctx.config.kb_dataset_ids,
            kb_mode=ctx.config.kb_mode,
            web_search_enabled=ctx.config.web_search_enabled,
            available_tools=available_tool_names or None,
            dataset_name_map=dataset_name_map,
            os_agent_enabled=ctx.config.os_agent_enabled,
            capabilities_enabled=capabilities_enabled,
        )
        # A synthesis-only call has a hard transport ceiling of ``tools=None``.
        # Do not let an evaluation override re-advertise capabilities that the
        # call cannot actually invoke.
        trusted_eval_prompt = (
            (ctx.config.eval_system_prompt_override or "").strip() if capabilities_enabled else ""
        )
        system_prompt = ensure_external_content_boundary(trusted_eval_prompt or base_prompt)
        candidate_system_prompt = ensure_external_content_boundary(
            trusted_eval_prompt
            or get_streaming_first_prompt(
                available_datasets=ctx.config.kb_dataset_ids,
                kb_mode=ctx.config.kb_mode,
                web_search_enabled=ctx.config.web_search_enabled,
                available_tools=None,
                dataset_name_map=dataset_name_map,
                os_agent_enabled=ctx.config.os_agent_enabled,
                capabilities_enabled=capabilities_enabled,
            )
        )
        if ctx.config.agent_runtime is not None:
            effective_capability_instructions = ctx.config.trusted_capability_instructions
            if not capabilities_enabled:
                effective_capability_instructions = (
                    "This synthesis pass has no tools, knowledge-base retrieval, "
                    "web search, or local OS capabilities. Use only the supplied "
                    "conversation and source material, and never claim an external "
                    "action was performed."
                )
            system_prompt = compose_agent_system_prompt(
                platform_prompt=system_prompt,
                agent_instructions=ctx.config.trusted_agent_instructions,
                channel_instructions=ctx.config.trusted_channel_instructions,
                capability_instructions=effective_capability_instructions,
            )
            candidate_system_prompt = compose_agent_system_prompt(
                platform_prompt=candidate_system_prompt,
                agent_instructions=ctx.config.trusted_agent_instructions,
                channel_instructions=ctx.config.trusted_channel_instructions,
                capability_instructions=effective_capability_instructions,
            )
        return system_prompt, stable_cache_hash(candidate_system_prompt)

    def _compile_auxiliary_context_packet(
        self,
        ctx: AgentLoopContext,
        *,
        messages: list[dict[str, Any]],
        purpose: str,
        fresh: bool,
        current_query: str | None = None,
        current_context: str | None = None,
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Compile every auxiliary model call through the same Packet boundary."""

        if not ctx.config.use_context_engine:
            return list(messages), None

        dimensions = {
            **ctx.context_cache_dimensions,
            "model": ctx.config.model_id,
            "auxiliary_call": purpose,
        }
        if "permission_snapshot" not in dimensions:
            allowlist = getattr(ctx.config, "capability_allowlist", None)
            dimensions["permission_snapshot"] = (
                sorted(allowlist.tool_names)
                if allowlist is not None
                else "legacy-no-explicit-allowlist"
            )
        dimensions.setdefault("rule_revision", {"auxiliary_call": purpose})
        if not fresh and ctx.context_packet is not None and ctx.context_assembler is not None:
            packet = ctx.context_assembler.bind_model_boundary(
                packet=ctx.context_packet,
                messages=messages,
                tool_definitions=[],
                trusted_system_prompt=str(messages[0].get("content") or ""),
                cache_dimensions=dimensions,
                previous_cache_receipt=ctx.context_packet_receipt,
            )
        else:
            normalized = [dict(message) for message in messages]
            if not normalized or normalized[0].get("role") != "system":
                raise ContextPacketIntegrityError(
                    "auxiliary context requires one leading trusted system message"
                )
            if current_query is None:
                current = normalized[-1]
                if current.get("role") != "user":
                    raise ContextPacketIntegrityError(
                        "fresh auxiliary context requires one terminal user request"
                    )
                query = str(current.get("content") or "")
                images = list(current.get("images") or [])
                auxiliary_history = normalized[1:-1]
            else:
                query = current_query
                images = []
                auxiliary_history = normalized[1:]
            model_info = self.model_registry.get_model(ctx.config.model_id)
            provider = str(
                getattr(getattr(model_info, "provider", None), "value", None) or "openai"
            )
            assembler = ContextAssemblerV2(
                provider=provider,
                budget_manager=ContextBudgetManager(
                    reserved_output_tokens=min(ctx.config.max_tokens or 2048, 2048),
                    min_recent_messages=0,
                    max_history_tokens=ctx.config.max_history_tokens,
                ),
            )
            packet = assembler.build_packet(
                context=ContextStructure(
                    system_prompt=str(normalized[0].get("content") or ""),
                    tool_definitions=[],
                    conversation_history=auxiliary_history,
                    task_state=bounded_working_memory_context(getattr(ctx, "working_memory", None)),
                    current_context=current_context,
                    current_query=query,
                    current_images=images,
                ),
                model_context_window=int(getattr(model_info, "context_window", 0) or 128000),
                tool_definitions=[],
                source_summaries=source_summaries,
                tool_result_summaries=tool_result_summaries,
                cache_dimensions=dimensions,
            )
            ctx.context_assembler = assembler

        ctx.context_packet = packet
        ctx.context_packet_receipt = packet.receipt()
        ctx.context_cache_dimensions = dimensions
        return packet.materialize_messages(), ctx.context_packet_receipt
