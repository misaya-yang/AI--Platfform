from __future__ import annotations

import asyncio
from typing import Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

from .evaluator_executor import EvaluatorExecutor
from .online_sampling import schedule_online_eval_for_trace

logger = get_logger(__name__)


class EvalOutboxWorker:
    """Poll agent_trace_outbox and execute eval jobs off the request path."""

    def __init__(
        self,
        repository: AgentTraceRepository,
        executor: EvaluatorExecutor,
        *,
        poll_interval_s: float = 2.0,
        batch_size: int = 4,
        max_attempts: int = 5,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.poll_interval_s = poll_interval_s
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, *, concurrency: int = 2) -> None:
        if self._running:
            return
        self._running = True
        workers = max(1, concurrency)
        self._tasks = [
            asyncio.create_task(self._poll_loop(worker_id=index)) for index in range(workers)
        ]
        logger.info("EvalOutboxWorker started with %s workers", workers)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("EvalOutboxWorker stopped")

    async def _poll_loop(self, *, worker_id: int) -> None:
        while self._running:
            try:
                jobs = await self.repository.claim_outbox_jobs(
                    limit=self.batch_size,
                    max_attempts=self.max_attempts,
                )
                if not jobs:
                    await asyncio.sleep(self.poll_interval_s)
                    continue
                for job in jobs:
                    if not self._running:
                        break
                    await self._handle_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                logger.warning("EvalOutboxWorker %s poll error: %s", worker_id, exc)
                await asyncio.sleep(self.poll_interval_s)

    async def _handle_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        tenant_id = str(job.get("tenant_id") or "")
        job_type = str(job.get("job_type") or "")
        payload = job.get("payload") or {}
        try:
            if job_type == "eval.evaluator.run":
                result = await self.executor.run_job(tenant_id=tenant_id, job_payload=payload)
                if result.status == "failed":
                    raise RuntimeError(result.error_message or "evaluator run failed")
            elif job_type == "trace.ingested":
                await schedule_online_eval_for_trace(
                    self.repository,
                    tenant_id=tenant_id,
                    payload=payload if isinstance(payload, dict) else {},
                    created_by="eval-online-sampler",
                )
            else:
                logger.info("Skipping unsupported outbox job_type=%s", job_type)
            await self.repository.mark_outbox_succeeded(job_id)
        except Exception as exc:  # noqa: BLE001 - retry via outbox state
            attempts = int(job.get("attempts") or 1)
            retry_after = min(300, 5 * attempts)
            terminal = attempts >= self.max_attempts
            await self.repository.mark_outbox_failed(
                job_id,
                error=str(exc),
                retry_after_seconds=retry_after,
                max_attempts=self.max_attempts,
            )
            run_id = str(payload.get("run_id") or "") if isinstance(payload, dict) else ""
            if run_id and job_type == "eval.evaluator.run":
                await self.repository.update_experiment_run(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    status="failed" if terminal else "queued",
                    error_message=str(exc)[:4000],
                    mark_finished=terminal,
                )
            logger.warning("Eval outbox job %s failed (attempt %s): %s", job_id, attempts, exc)
