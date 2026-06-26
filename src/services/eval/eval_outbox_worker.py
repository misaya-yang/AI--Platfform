from __future__ import annotations

from typing import Any

from ai_gateway_core.eval import EvalOutboxWorker, EvaluatorExecutor
from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

logger = get_logger(__name__)

_eval_outbox_worker: EvalOutboxWorker | None = None


def init_eval_outbox_worker(
    database: Any,
    *,
    enabled: bool = True,
    concurrency: int = 2,
    poll_interval_s: float = 2.0,
) -> EvalOutboxWorker | None:
    global _eval_outbox_worker
    if not enabled or not getattr(database, "enabled", False):
        _eval_outbox_worker = None
        return None
    repository = AgentTraceRepository(database)
    executor = EvaluatorExecutor(repository, created_by="eval-worker")
    _eval_outbox_worker = EvalOutboxWorker(
        repository,
        executor,
        poll_interval_s=poll_interval_s,
    )
    return _eval_outbox_worker


def get_eval_outbox_worker() -> EvalOutboxWorker | None:
    return _eval_outbox_worker
