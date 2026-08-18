"""Completed-turn synchronization for source-first runtime memory."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception
from ai_gateway_core.security.redaction import redact_trace_text

from .background_sync import OrderedBackgroundSync
from .lifecycle import MemoryProviderLifecycle, MemoryWriteResult, should_sync_turn_to_memory

logger = get_logger(__name__)


@dataclass
class MemoryTurnSyncResult:
    """Receipt for one source commit and its queued derivatives."""

    synced: bool
    skipped: bool
    reason: str
    write: MemoryWriteResult | None = None
    index_result: Any | None = None
    pii_findings: list[str] | None = None
    source_committed: bool = False
    index_pending: bool = False
    retryable: bool = False
    errors: tuple[str, ...] = ()
    background_operation_id: str | None = None

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
            "source_committed": self.source_committed,
            "index_pending": self.index_pending,
            "retryable": self.retryable,
            "errors": list(self.errors),
            "background_operation_id": self.background_operation_id,
        }


class CompletedTurnMemorySync:
    """Commit completed turns locally, then refresh derivatives in order."""

    def __init__(
        self,
        *,
        memory_store: Any,
        memory_indexer: Any,
        pii_filter: Any,
        lifecycle: MemoryProviderLifecycle,
        background: OrderedBackgroundSync | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.memory_indexer = memory_indexer
        self.pii_filter = pii_filter
        self.lifecycle = lifecycle
        self.background = background or OrderedBackgroundSync()

    async def sync(
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
        allowed, reason = should_sync_turn_to_memory(
            terminal_envelope,
            explicit_opt_in=explicit_opt_in,
        )
        if not allowed:
            return MemoryTurnSyncResult(synced=False, skipped=True, reason=reason)

        user_text = str(user_message or "").strip()
        assistant_text = str(assistant_message or "").strip()
        if not user_text:
            return MemoryTurnSyncResult(
                synced=False,
                skipped=True,
                reason="completed_turn_user_message_missing",
            )
        if not assistant_text:
            return MemoryTurnSyncResult(
                synced=False,
                skipped=True,
                reason="completed_turn_assistant_message_missing",
            )

        snapshot = f"User: {user_text}\n\nAssistant: {assistant_text}"[:6000]
        secret_redacted = redact_trace_text(snapshot)
        redacted_text, findings = self.pii_filter.redact(secret_redacted)
        write, source_document, source_handle = await asyncio.to_thread(
            self.memory_store.append_daily_entry_and_read_result,
            tenant_id,
            user_id,
            redacted_text,
        )
        operation_id = f"memsync_{uuid.uuid4().hex[:16]}"
        metadata = {
            "run_id": (terminal_envelope or {}).get("run_id"),
            "session_id": session_id,
            "source_type": write.source_type,
            "memory_layer": "durable_daily",
            "terminal_exit_reason": (terminal_envelope or {}).get("exit_reason"),
            "pii_findings": [finding.pattern for finding in findings],
            "write": write.to_dict(),
            "source_handle": source_handle,
        }

        async def sync_derivatives() -> dict[str, Any]:
            errors: list[str] = []
            try:
                await self.lifecycle.on_memory_write(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    write=write.to_dict(),
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.turn_sync.internal_failure", exc
                )
                errors.append("memory_write_notification_pending")

            try:
                index_result = await self.memory_indexer.index_source(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_path=write.path,
                    source_type=write.source_type,
                    content=source_document.content,
                    metadata=metadata,
                    updated_at=source_document.updated_at,
                )
                if getattr(index_result, "fallback_reason", None):
                    errors.append("memory_vector_index_pending")
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.turn_sync.internal_failure", exc
                )
                errors.append("memory_index_pending")

            try:
                await self.lifecycle.sync_turn(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    terminal_envelope=terminal_envelope,
                    write=write.to_dict(),
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.runtime.memory.turn_sync.internal_failure", exc
                )
                errors.append("memory_provider_sync_pending")

            return {
                "status": "completed" if not errors else "partial",
                "source_committed": True,
                "index_pending": any("index_pending" in error for error in errors),
                "errors": errors,
            }

        self.background.enqueue(
            key=(tenant_id, user_id, write.path),
            operation_id=operation_id,
            work=sync_derivatives,
        )
        return MemoryTurnSyncResult(
            synced=True,
            skipped=False,
            reason="completed_turn_source_committed",
            write=write,
            pii_findings=[finding.pattern for finding in findings],
            source_committed=True,
            index_pending=True,
            background_operation_id=operation_id,
        )

    async def flush_pending(self, *, timeout: float | None = None) -> dict[str, Any]:
        return await self.background.flush_pending(timeout=timeout)

    def status(self, operation_id: str) -> dict[str, Any] | None:
        return self.background.receipt(operation_id)

    def source_is_pending(self, tenant_id: str, user_id: str, source_path: str) -> bool:
        return self.background.is_pending((tenant_id, user_id, source_path))
