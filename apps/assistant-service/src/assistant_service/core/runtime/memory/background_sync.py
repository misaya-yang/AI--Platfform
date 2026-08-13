"""Ordered best-effort synchronization for derived memory stores."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Any

from ai_gateway_core.logging import get_logger, record_internal_exception

logger = get_logger(__name__)

_MAX_RETAINED_RECEIPTS = 256
_MAX_PENDING_SYNCS = 128


@dataclass
class BackgroundSyncReceipt:
    """Prompt-free status for one queued derivative sync."""

    operation_id: str
    status: str = "queued"
    queued_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "queued_at": self.queued_at,
            "finished_at": self.finished_at,
            "result": dict(self.result),
            "error_code": self.error_code,
        }


class OrderedBackgroundSync:
    """Run work in order per source without holding the request open.

    The markdown source remains authoritative.  This queue only updates SQL,
    vector, or provider derivatives, so a cancelled process can rebuild them
    from the source on the next read.
    """

    def __init__(
        self,
        *,
        max_pending: int = _MAX_PENDING_SYNCS,
        max_retained_receipts: int = _MAX_RETAINED_RECEIPTS,
    ) -> None:
        if max_pending < 1 or max_retained_receipts < 1:
            raise ValueError("background sync limits must be positive")
        self._max_pending = max_pending
        self._max_retained_receipts = max_retained_receipts
        self._tails: dict[Hashable, asyncio.Task[None]] = {}
        self._receipts: OrderedDict[str, BackgroundSyncReceipt] = OrderedDict()
        self._operation_tasks: dict[str, asyncio.Task[None]] = {}
        self._unreported: set[str] = set()

    def enqueue(
        self,
        *,
        key: Hashable,
        operation_id: str,
        work: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> BackgroundSyncReceipt:
        if operation_id in self._receipts:
            raise ValueError("background sync operation_id must be unique")
        previous = self._tails.get(key)
        receipt = BackgroundSyncReceipt(operation_id=operation_id)
        self._receipts[operation_id] = receipt
        self._unreported.add(operation_id)
        pending_count = sum(not task.done() for task in self._operation_tasks.values())
        if pending_count >= self._max_pending:
            receipt.status = "deferred"
            receipt.finished_at = time.time()
            receipt.error_code = "memory_background_sync_capacity"
            receipt.result = {
                "status": "partial",
                "source_committed": True,
                "index_pending": True,
                "errors": ["memory_background_sync_capacity"],
            }
            self._prune_receipts()
            return receipt
        task = asyncio.create_task(
            self._run(previous=previous, receipt=receipt, work=work),
            name=f"memory-sync:{operation_id}",
        )
        self._tails[key] = task
        self._operation_tasks[operation_id] = task
        task.add_done_callback(lambda completed: self._discard_tail(key, operation_id, completed))
        self._prune_receipts()
        return receipt

    async def _run(
        self,
        *,
        previous: asyncio.Task[None] | None,
        receipt: BackgroundSyncReceipt,
        work: Callable[[], Awaitable[dict[str, Any] | None]],
    ) -> None:
        try:
            if previous is not None:
                await previous
            receipt.status = "running"
            receipt.result = dict(await work() or {})
            receipt.status = str(receipt.result.get("status") or "completed")
        except asyncio.CancelledError:
            receipt.status = "cancelled"
            receipt.error_code = "memory_background_sync_cancelled"
            raise
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.runtime.memory.background_sync.internal_failure", exc
            )
            receipt.status = "failed"
            receipt.error_code = "memory_background_sync_failed"
        finally:
            receipt.finished_at = time.time()

    def _discard_tail(
        self,
        key: Hashable,
        operation_id: str,
        task: asyncio.Task[None],
    ) -> None:
        receipt = self._receipts.get(operation_id)
        if task.cancelled() and receipt is not None and receipt.finished_at is None:
            receipt.status = "cancelled"
            receipt.error_code = "memory_background_sync_cancelled"
            receipt.finished_at = time.time()
        if self._tails.get(key) is task:
            self._tails.pop(key, None)
        if self._operation_tasks.get(operation_id) is task:
            self._operation_tasks.pop(operation_id, None)
        self._prune_receipts()

    def _prune_receipts(self) -> None:
        overflow = len(self._receipts) - self._max_retained_receipts
        for operation_id, receipt in list(self._receipts.items()):
            if overflow <= 0:
                break
            if receipt.finished_at is None:
                continue
            self._receipts.pop(operation_id, None)
            self._operation_tasks.pop(operation_id, None)
            self._unreported.discard(operation_id)
            overflow -= 1

    def is_pending(self, key: Hashable) -> bool:
        task = self._tails.get(key)
        return task is not None and not task.done()

    def receipt(self, operation_id: str) -> dict[str, Any] | None:
        receipt = self._receipts.get(operation_id)
        return receipt.to_dict() if receipt is not None else None

    async def flush_pending(self, *, timeout: float | None = None) -> dict[str, Any]:
        operation_ids = list(self._unreported)
        tasks = [
            self._operation_tasks[operation_id]
            for operation_id in operation_ids
            if operation_id in self._operation_tasks
        ]
        if tasks:
            if timeout is None:
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))
        receipts = [
            self._receipts[operation_id]
            for operation_id in operation_ids
            if operation_id in self._receipts
        ]
        counts: dict[str, int] = {}
        for receipt in receipts:
            counts[receipt.status] = counts.get(receipt.status, 0) + 1
            if receipt.finished_at is not None:
                self._unreported.discard(receipt.operation_id)
        pending_count = sum(
            count for status, count in counts.items() if status in {"queued", "running"}
        )
        incomplete_count = sum(
            count for status, count in counts.items() if status not in {"completed", "succeeded"}
        )
        self._prune_receipts()
        return {
            "status": (
                "pending"
                if pending_count or any(not task.done() for task in tasks)
                else "partial"
                if incomplete_count
                else "completed"
            ),
            "pending": max(pending_count, sum(not task.done() for task in tasks)),
            "counts": counts,
        }
