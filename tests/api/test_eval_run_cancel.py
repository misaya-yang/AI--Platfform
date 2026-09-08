from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.v1 import eval as routes


@pytest.mark.asyncio
async def test_cancel_fences_job_before_interrupt_and_uses_persisted_actor(monkeypatch):
    order = []

    class Repository:
        async def cancel_experiment_run(self, **kwargs):
            assert kwargs == {"tenant_id": "tenant", "run_id": "run", "cancelled_by": "operator"}
            order.append("fenced")
            return {"run_id": "run", "status": "cancelled", "cases": [{
                "run_case_id": "case", "runtime_handle": {
                    "thread_id": "thread", "turn_id": "turn", "user_id": "candidate-actor",
                },
            }]}

    async def interrupt(**kwargs):
        assert order == ["fenced"]
        assert kwargs["tenant_id"] == "tenant"
        assert kwargs["user_id"] == "candidate-actor"
        assert kwargs["session_id"] == "case"
        order.append("interrupt")

    monkeypatch.setattr(routes, "_get_trace_repository", lambda _request: Repository())
    monkeypatch.setattr(routes, "_require_eval_run_access", lambda *_args: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        agent_runtime_control=SimpleNamespace(interrupt_turn=interrupt),
    )))
    result = await routes.cancel_eval_experiment_run("run", request, SimpleNamespace(tenant_id="tenant", user_id="operator"))
    assert result == {"run_id": "run", "status": "cancelled", "runtime_interrupt_pending": 0}


@pytest.mark.asyncio
async def test_cross_tenant_unknown_run_does_not_interrupt(monkeypatch):
    repo = SimpleNamespace(cancel_experiment_run=AsyncMock(return_value=None))
    interrupt = AsyncMock()
    monkeypatch.setattr(routes, "_get_trace_repository", lambda _request: repo)
    monkeypatch.setattr(routes, "_require_eval_run_access", lambda *_args: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        agent_runtime_control=SimpleNamespace(interrupt_turn=interrupt),
    )))
    with pytest.raises(HTTPException) as error:
        await routes.cancel_eval_experiment_run("run", request, SimpleNamespace(tenant_id="other", user_id="operator"))
    assert error.value.status_code == 404
    interrupt.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_interrupt_is_visible_and_safe_to_retry(monkeypatch):
    async def cancelled(**_kwargs):
        return {"run_id": "run", "status": "cancelled", "cases": [{
            "run_case_id": "case", "runtime_handle": {}, "dispatch_state": "reconcile_required",
        }]}

    monkeypatch.setattr(routes, "_get_trace_repository", lambda _request: SimpleNamespace(cancel_experiment_run=cancelled))
    monkeypatch.setattr(routes, "_require_eval_run_access", lambda *_args: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    result = await routes.cancel_eval_experiment_run("run", request, SimpleNamespace(tenant_id="tenant", user_id="operator"))
    assert result["runtime_interrupt_pending"] == 1
