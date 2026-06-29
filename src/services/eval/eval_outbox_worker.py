from __future__ import annotations

from typing import Any

from ai_gateway_core.eval import EvalOutboxWorker, EvaluatorExecutor
from ai_gateway_core.logging import get_logger
from ai_gateway_core.persistence.repositories.agent_trace_repository import AgentTraceRepository

from .eval_llm_client import build_eval_llm_complete, load_eval_llm_settings
from .kb_ragas_client import KbRagasClient, build_kb_ragas_complete

logger = get_logger(__name__)

_eval_outbox_worker: EvalOutboxWorker | None = None
_kb_ragas_client: KbRagasClient | None = None


async def _kb_ragas_evaluate(**kwargs: Any) -> list[dict[str, Any]]:
    global _kb_ragas_client
    if _kb_ragas_client is None:
        _kb_ragas_client = build_kb_ragas_complete()
    results = await _kb_ragas_client.evaluate_retrieval(**kwargs)
    return [
        {
            "metric": item.metric,
            "score": item.score,
            "explanation": item.explanation,
            "label": item.label,
            "judge_model": item.judge_model,
        }
        for item in results
    ]


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
    llm_settings = load_eval_llm_settings()
    llm_complete = build_eval_llm_complete(llm_settings)
    executor = EvaluatorExecutor(
        repository,
        llm_complete=llm_complete,
        kb_ragas_evaluate=_kb_ragas_evaluate,
        created_by="eval-worker",
    )
    if llm_complete is not None:
        logger.info(
            "Eval LLM judge enabled model=%s assistant=%s",
            llm_settings.default_judge_model_id,
            llm_settings.assistant_base_url,
        )
    else:
        logger.warning("Eval LLM judge disabled; llm evaluators will require manual review")
    _eval_outbox_worker = EvalOutboxWorker(
        repository,
        executor,
        poll_interval_s=poll_interval_s,
    )
    return _eval_outbox_worker


def get_eval_outbox_worker() -> EvalOutboxWorker | None:
    return _eval_outbox_worker
