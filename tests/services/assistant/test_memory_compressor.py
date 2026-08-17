from __future__ import annotations

import copy
from typing import Any

import pytest
from assistant_service.core.memory.compressor import ContextCompressor, ModelRegistryLLMService
from assistant_service.core.run_budget import RunBudgetDimension, RunBudgetExceeded


class _CapturingLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt: str, max_tokens: int = 200) -> str:
        del max_tokens
        self.prompts.append(prompt)
        return f"summary stage {len(self.prompts)}"


@pytest.mark.asyncio
async def test_model_registry_llm_service_propagates_run_budget_exceeded() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.called = False

        async def chat(self, *_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
            self.called = True
            return "must not execute", {}

    budget_error = RunBudgetExceeded(
        dimension=RunBudgetDimension.MODEL_TURNS,
        limit=1,
        used=1,
        requested=2,
        snapshot={"status": "exhausted"},
    )
    registry = _Registry()

    def reject_model_turn() -> None:
        raise budget_error

    service = ModelRegistryLLMService(
        registry,
        model_id="test",
        before_complete=reject_model_turn,
    )

    with pytest.raises(RunBudgetExceeded) as caught:
        await service.complete("summarize")

    assert caught.value is budget_error
    assert registry.called is False


@pytest.mark.asyncio
async def test_context_compressor_propagates_run_budget_exceeded() -> None:
    budget_error = RunBudgetExceeded(
        dimension=RunBudgetDimension.MODEL_TURNS,
        limit=1,
        used=1,
        requested=2,
        snapshot={"status": "exhausted"},
    )

    class _BudgetFailingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, prompt: str, max_tokens: int = 200) -> str:
            del prompt, max_tokens
            self.calls += 1
            raise budget_error

    llm = _BudgetFailingLLM()
    compressor = ContextCompressor(llm)

    with pytest.raises(RunBudgetExceeded) as caught:
        await compressor.compress(
            [
                {"role": "user", "content": "old context"},
                {"role": "user", "content": "current request"},
            ],
            target_tokens=500,
            preserve_recent=1,
        )

    assert caught.value is budget_error
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_compressor_redacts_secrets_and_preserves_non_sensitive_identifiers() -> None:
    llm = _CapturingLLM()
    compressor = ContextCompressor(llm)
    uuid_value = "550e8400-e29b-41d4-a716-446655440000"
    hash_value = "a1b2c3d4e5f60718293a4b5c"
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "api_key=top-secret-value inspect report-final.pdf on kb.internal.example.com "
                f"for CASE-1042 object {uuid_value} digest {hash_value}"
            ),
        },
        {"role": "assistant", "content": "acknowledged"},
        {"role": "user", "content": "current request"},
    ]

    result = await compressor.compress(messages, target_tokens=500, preserve_recent=1)

    assert "top-secret-value" not in "\n".join(llm.prompts)
    assert "api_key=[redacted]" in llm.prompts[0]
    assert uuid_value in result.preserved_identifiers
    assert hash_value in result.preserved_identifiers
    assert "report-final.pdf" in result.preserved_identifiers
    assert "kb.internal.example.com" in result.preserved_identifiers
    assert "CASE-1042" in result.preserved_identifiers
    assert all("top-secret-value" not in item for item in result.preserved_identifiers)
    assert "## Goal" in llm.prompts[0]
    assert "## Artifacts" in llm.prompts[0]
    assert "## Open tasks" in llm.prompts[0]


@pytest.mark.asyncio
async def test_staged_summary_only_runs_when_enabled_and_source_is_long() -> None:
    long_messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index) + "x" * 5000}
        for index in range(6)
    ] + [{"role": "user", "content": "current"}]

    one_pass_llm = _CapturingLLM()
    one_pass = await ContextCompressor(one_pass_llm).compress(
        long_messages,
        target_tokens=500,
        preserve_recent=1,
        staged=False,
        staged_min_source_tokens=1000,
    )
    assert one_pass.summary_stages == 1
    assert len(one_pass_llm.prompts) == 1

    staged_llm = _CapturingLLM()
    staged = await ContextCompressor(staged_llm).compress(
        long_messages,
        target_tokens=500,
        preserve_recent=1,
        staged=True,
        staged_min_source_tokens=1000,
    )
    assert staged.summary_stages > 2
    assert len(staged_llm.prompts) == staged.summary_stages


@pytest.mark.asyncio
async def test_compaction_rejects_less_than_ten_percent_savings_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant_service.core.agent import agent_loop as agent_loop_module
    from assistant_service.core.agent.agent_loop import AgentLoop

    class _Registry:
        async def chat(self, *_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
            return "valid compact summary", {}

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _Registry()
    messages = [
        {"role": "system", "content": "trusted"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "working"},
    ]
    original = copy.deepcopy(messages)
    original_ids = [id(message) for message in messages]

    monkeypatch.setattr(
        agent_loop_module,
        "estimate_history_tokens",
        lambda candidate: 1000 if len(candidate) >= 5 else 950,
    )
    stats = await loop._compact_messages_by_turns(
        messages,
        keep_recent_turns=1,
        model_id="qwen3.7-plus",
    )

    assert stats["compacted"] is False
    assert stats["reason"] == "insufficient_token_savings"
    assert messages == original
    assert [id(message) for message in messages] == original_ids


@pytest.mark.asyncio
async def test_compaction_redacts_protected_constraint_and_plan_before_commit() -> None:
    from assistant_service.core.agent.agent_loop import AgentLoop

    class _Registry:
        async def chat(self, *_args: Any, **_kwargs: Any) -> tuple[str, dict[str, Any]]:
            return "The prior audit remains incomplete.", {}

    loop = AgentLoop.__new__(AgentLoop)
    loop.model_registry = _Registry()
    messages = [
        {"role": "system", "content": "trusted"},
        {"role": "user", "content": "old context " * 500},
        {"role": "assistant", "content": "old response"},
        {
            "role": "user",
            "content": "MUST preserve audit state; password=do-not-copy",
        },
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "working"},
    ]

    stats = await loop._compact_messages_by_turns(
        messages,
        keep_recent_turns=1,
        model_id="qwen3.7-plus",
        protected_plan={"task": "audit", "token": "plan-secret"},
    )

    assert stats["compacted"] is True
    summary = messages[1]["content"]
    assert "do-not-copy" not in summary
    assert "plan-secret" not in summary
    assert "password=[redacted]" in summary
    assert '"token": \\"[redacted]\\"' in summary
