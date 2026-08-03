"""
Sub-Agent Manager — orchestrates explore/task/plan sub-agents.

ADR-003: Creates isolated sub-agents with their own context windows,
runs them to completion, and returns summarized results to the main agent.
SSE events are yielded for real-time frontend display.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

from .stream_helpers import merge_stream_tool_calls
from .subagent_types import (
    SUBAGENT_DEFAULTS,
    SubAgentConfig,
    SubAgentState,
    SubAgentStep,
    SubAgentType,
)

if TYPE_CHECKING:
    from ..gateway.execution_gateway import AssistantExecutionGateway
    from ..models.model_registry import ModelRegistry
    from ..tool_invoker import ToolInvocationContext, ToolInvoker
    from ..tools.tool_registry import ToolRegistry

from ..run_budget import RunBudget, RunBudgetExceeded

logger = get_logger(__name__)


class _ParentCancelled(Exception):
    """Controlled parent cancellation; unlike task cancellation it has a terminal event."""


class SubAgentManager:
    """
    Orchestrate sub-agents with isolated contexts.

    Each sub-agent gets:
    - Independent message history (no parent history leakage)
    - Filtered tool set (explore = read-only, task = full)
    - Its own LLM loop (simplified, no 8-step preprocessing)
    - SSE event stream for real-time UI updates
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        tool_registry: ToolRegistry,
        tool_invoker: ToolInvoker | None = None,
        execution_gateway: AssistantExecutionGateway | None = None,
    ) -> None:
        from ..tool_invoker import create_tool_invoker

        self.model_registry = model_registry
        self.tool_registry = tool_registry
        self.tool_invoker = tool_invoker or create_tool_invoker(tool_registry=tool_registry)
        self.execution_gateway = execution_gateway
        self._active: dict[str, SubAgentState] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def spawn(
        self,
        config: SubAgentConfig,
        parent_user: Any | None = None,
        parent_tenant_id: str = "",
        kb_dataset_ids: list[str] | None = None,
        parent_invocation_context: ToolInvocationContext | None = None,
        parent_cancel_event: asyncio.Event | None = None,
        parent_attempt_id: str = "",
        parent_model_id: str | None = None,
        parent_max_turns: int | None = None,
        parent_max_tool_calls: int | None = None,
        parent_max_tokens: int | None = None,
        parent_timeout_seconds: float | None = None,
        run_budget: RunBudget | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Spawn a single sub-agent. Yields SSE-compatible event dicts."""
        agent_id = f"sub_{uuid.uuid4().hex[:12]}"
        defaults = SUBAGENT_DEFAULTS.get(config.agent_type, {})

        state = SubAgentState(
            agent_id=agent_id,
            agent_type=config.agent_type,
            description=config.description or config.prompt[:50],
            status="running",
            started_at=time.time(),
        )
        self._active[agent_id] = state

        attempt_id = parent_attempt_id or str(
            ((parent_invocation_context.metadata or {}).get("attempt_id") or "")
            if parent_invocation_context is not None
            else ""
        )
        yield {
            "event_type": "subagent_started",
            "data": {
                "agent_id": agent_id,
                "attempt_id": attempt_id,
                "agent_type": config.agent_type.value,
                "description": state.description,
                "prompt": config.prompt[:200],
            },
        }

        result_text = ""
        effective_config = config
        try:
            effective_config = self._bounded_config(
                config,
                defaults,
                parent_max_turns=parent_max_turns,
                parent_max_tool_calls=parent_max_tool_calls,
                parent_max_tokens=parent_max_tokens,
                parent_timeout_seconds=parent_timeout_seconds,
            )
            deadline = asyncio.get_running_loop().time() + effective_config.timeout_seconds
            messages = self._build_messages(config)
            tools, invocation_context = await self._await_with_controls(
                self._get_tools(
                    effective_config,
                    defaults,
                    parent_user,
                    agent_id=agent_id,
                    parent_tenant_id=parent_tenant_id,
                    parent_invocation_context=parent_invocation_context,
                    kb_dataset_ids=kb_dataset_ids,
                ),
                cancel_event=parent_cancel_event,
                deadline=deadline,
            )
            invocation_context.metadata["attempt_id"] = attempt_id
            model_id = parent_model_id or self._pick_model(effective_config)
            system_prompt = self._build_system_prompt(effective_config, defaults)

            recovery: dict[str, Any] | None = None
            async for event in self._run_loop(
                agent_id,
                state,
                messages,
                tools,
                model_id,
                system_prompt,
                effective_config,
                defaults,
                kb_dataset_ids=invocation_context.kb_dataset_ids,
                invocation_context=invocation_context,
                cancel_event=parent_cancel_event,
                deadline=deadline,
                attempt_id=attempt_id,
                run_budget=run_budget,
            ):
                yield event
                if event["event_type"] == "subagent_text_delta":
                    result_text += event["data"].get("text", "")
                elif event["event_type"] == "subagent_side_effect_unknown":
                    recovery = dict(event["data"])

            if state.status == "blocked":
                yield self._terminal_event(
                    state,
                    status="blocked",
                    result_text=result_text,
                    error=state.error or "SIDE_EFFECT_UNKNOWN",
                    attempt_id=attempt_id,
                    recovery=recovery,
                )
                return

            if state.error or not result_text.strip():
                yield self._terminal_event(
                    state,
                    status="failed",
                    result_text=result_text,
                    error=state.error or "Sub-agent returned no result",
                    attempt_id=attempt_id,
                )
                return

            yield self._terminal_event(
                state,
                status="completed",
                result_text=result_text,
                attempt_id=attempt_id,
            )

        except RunBudgetExceeded:
            raise
        except asyncio.TimeoutError:
            yield self._terminal_event(
                state,
                status="failed",
                result_text=result_text,
                error=f"Timeout after {effective_config.timeout_seconds:g}s",
                attempt_id=attempt_id,
            )
        except _ParentCancelled:
            yield self._terminal_event(
                state,
                status="cancelled",
                result_text=result_text,
                error="Cancelled by parent",
                attempt_id=attempt_id,
            )
        except Exception as e:
            logger.error(
                "SubAgent %s failed (exception_type=%s)",
                agent_id,
                type(e).__name__,
            )
            yield self._terminal_event(
                state,
                status="failed",
                result_text=result_text,
                error=f"Sub-agent execution failed ({type(e).__name__})",
                attempt_id=attempt_id,
            )
        finally:
            self._active.pop(agent_id, None)

    async def spawn_parallel(
        self,
        configs: list[SubAgentConfig],
        parent_user: Any | None = None,
        parent_tenant_id: str = "",
        kb_dataset_ids: list[str] | None = None,
        parent_invocation_context: ToolInvocationContext | None = None,
        parent_cancel_event: asyncio.Event | None = None,
        parent_attempt_id: str = "",
        parent_model_id: str | None = None,
        parent_max_turns: int | None = None,
        parent_max_tool_calls: int | None = None,
        parent_max_tokens: int | None = None,
        parent_timeout_seconds: float | None = None,
        max_concurrency: int = 5,
        run_budget: RunBudget | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Spawn multiple sub-agents in parallel. Merges event streams."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        done_count = 0
        total = len(configs)

        concurrency = max(1, min(int(max_concurrency), 10))
        semaphore = asyncio.Semaphore(concurrency)
        combined_cancel = asyncio.Event()
        relay_task: asyncio.Task[None] | None = None
        if parent_cancel_event is not None:

            async def _relay_cancel() -> None:
                await parent_cancel_event.wait()
                combined_cancel.set()

            relay_task = asyncio.create_task(_relay_cancel())

        async def _run(cfg: SubAgentConfig) -> None:
            async with semaphore:
                async for event in self.spawn(
                    cfg,
                    parent_user,
                    parent_tenant_id,
                    kb_dataset_ids=kb_dataset_ids,
                    parent_invocation_context=parent_invocation_context,
                    parent_cancel_event=combined_cancel,
                    parent_attempt_id=parent_attempt_id,
                    parent_model_id=parent_model_id,
                    parent_max_turns=parent_max_turns,
                    parent_max_tool_calls=parent_max_tool_calls,
                    parent_max_tokens=parent_max_tokens,
                    parent_timeout_seconds=parent_timeout_seconds,
                    run_budget=run_budget,
                ):
                    await queue.put(event)

        tasks = [asyncio.create_task(_run(c)) for c in configs]

        blocked_recovery: dict[str, Any] | None = None
        try:
            while done_count < total:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield event
                    if event["event_type"] == "subagent_side_effect_unknown":
                        blocked_recovery = dict(event.get("data") or {})
                        combined_cancel.set()
                    if event["event_type"] == "subagent_finished":
                        done_count += 1
                except asyncio.TimeoutError:
                    done_count = sum(task.done() for task in tasks)
            if blocked_recovery is not None:
                yield {
                    "event_type": "subagent_parallel_blocked",
                    "data": {
                        "status": "blocked",
                        "reason": "side_effect_unknown",
                        "recovery": blocked_recovery,
                    },
                }
        finally:
            if relay_task is not None:
                relay_task.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if relay_task is not None:
                await asyncio.gather(relay_task, return_exceptions=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _bounded_config(
        config: SubAgentConfig,
        defaults: dict[str, Any],
        *,
        parent_max_turns: int | None,
        parent_max_tool_calls: int | None,
        parent_max_tokens: int | None,
        parent_timeout_seconds: float | None,
    ) -> SubAgentConfig:
        def _limit(name: str, parent_limit: int | float | None) -> int | float:
            requested = getattr(config, name)
            default_limit = defaults.get(name, requested)
            if (
                requested <= 0
                or default_limit <= 0
                or (parent_limit is not None and parent_limit <= 0)
            ):
                raise ValueError(f"{name} budget is exhausted")
            ceiling = default_limit if parent_limit is None else min(default_limit, parent_limit)
            return min(requested, ceiling)

        return dataclasses.replace(
            config,
            max_turns=int(_limit("max_turns", parent_max_turns)),
            max_tool_calls=int(_limit("max_tool_calls", parent_max_tool_calls)),
            max_tokens=int(_limit("max_tokens", parent_max_tokens)),
            timeout_seconds=float(_limit("timeout_seconds", parent_timeout_seconds)),
            # A child never selects a model outside the parent-supplied model.
            model_override=None,
        )

    @staticmethod
    async def _await_with_controls(
        awaitable: Any,
        *,
        cancel_event: asyncio.Event | None,
        deadline: float,
    ) -> Any:
        operation = asyncio.ensure_future(awaitable)
        cancellation = (
            asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        )
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            wait_for = {operation}
            if cancellation is not None:
                wait_for.add(cancellation)
            done, _ = await asyncio.wait(
                wait_for,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if cancellation is not None and cancellation in done and cancel_event.is_set():
                raise _ParentCancelled
            return await operation
        finally:
            pending = [
                task for task in (operation, cancellation) if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    @classmethod
    def _terminal_event(
        cls,
        state: SubAgentState,
        *,
        status: str,
        result_text: str,
        attempt_id: str,
        error: str | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state.status = status
        state.error = error
        state.result = result_text
        state.finished_at = time.time()
        state.duration_ms = (state.finished_at - (state.started_at or state.finished_at)) * 1000
        summary = cls._summarize(result_text)
        evidence = [
            {
                "tool_name": step.tool_name,
                "call_id": step.call_id,
                "status": step.status,
                "summary": step.summary or "",
            }
            for step in state.steps[:50]
        ]
        limitations = [
            f"Tool {step.tool_name} did not complete successfully"
            for step in state.steps
            if step.status != "completed"
        ][:20]
        if error:
            limitations.insert(0, error[:500])
        limitations = limitations[:20]
        result = {
            "status": status,
            "claims": [summary] if status == "completed" and summary else [],
            "evidence": evidence,
            "limitations": limitations,
            "attempt_id": attempt_id,
        }
        data: dict[str, Any] = {
            "agent_id": state.agent_id,
            "attempt_id": attempt_id,
            "status": status,
            "result_summary": summary,
            "result": result,
            "duration_ms": state.duration_ms,
            "turns": state.turns_completed,
            "tool_calls": state.tool_calls_made,
        }
        if error:
            data["error"] = error
        if recovery is not None:
            data.update(
                {
                    "side_effect_unknown": True,
                    "recovery": recovery,
                }
            )
        return {"event_type": "subagent_finished", "data": data}

    def _build_messages(self, config: SubAgentConfig) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if config.parent_context:
            messages.append(
                {"role": "user", "content": f"Context from parent agent:\n{config.parent_context}"}
            )
        messages.append({"role": "user", "content": config.prompt})
        return messages

    async def _get_tools(
        self,
        config: SubAgentConfig,
        defaults: dict,
        user: Any | None,
        *,
        agent_id: str,
        parent_tenant_id: str,
        parent_invocation_context: ToolInvocationContext | None,
        kb_dataset_ids: list[str] | None,
    ) -> tuple[list, ToolInvocationContext]:
        """Resolve a child catalog through the canonical authorization boundary."""

        del config
        from ..tool_invoker import (
            CapabilityAllowlist,
            ToolInvocationContext,
            ToolPolicySnapshot,
        )

        parent = parent_invocation_context
        if parent is None:
            user_id = str(getattr(user, "user_id", None) or getattr(user, "id", None) or "")
            invocation_context = ToolInvocationContext(
                session_id=f"subagent:{agent_id}",
                user_id=user_id,
                tenant_id=str(parent_tenant_id or ""),
                request_id=agent_id,
                run_id=None,
                parent_task_id=agent_id,
                kb_dataset_ids=list(kb_dataset_ids or []),
                user=user,
                capability_allowlist=CapabilityAllowlist(),
                metadata={"subagent_id": agent_id, "authority_resolved": False},
            )
            invocation_context.policy_snapshot = ToolPolicySnapshot.denied_for(invocation_context)
        else:
            parent_datasets = frozenset(str(value) for value in parent.kb_dataset_ids)
            requested_datasets = (
                parent_datasets
                if kb_dataset_ids is None
                else frozenset(str(value) for value in kb_dataset_ids)
            )
            inherited_metadata = {
                key: value
                for key, value in (parent.metadata or {}).items()
                if key
                not in {
                    "approval_consumed",
                    "execution_gateway_approved",
                    "gateway_policy_decision",
                    "idempotency_key",
                    "logical_operation_id",
                    "sandbox_decision",
                }
            }
            invocation_context = ToolInvocationContext(
                session_id=parent.session_id,
                user_id=parent.user_id,
                tenant_id=parent.tenant_id,
                request_id=parent.request_id,
                run_id=parent.run_id,
                timeout_ms=parent.timeout_ms,
                max_retries=parent.max_retries,
                parent_task_id=agent_id,
                scope_id=f"{parent.scope_id or parent.session_id}:subagent:{agent_id}",
                policy_profile=parent.policy_profile,
                os_agent_enabled=parent.os_agent_enabled,
                kb_dataset_ids=sorted(parent_datasets.intersection(requested_datasets)),
                user=parent.user,
                capability_allowlist=parent.capability_allowlist,
                policy_snapshot=parent.policy_snapshot,
                uncertain_operation_fingerprints=parent.uncertain_operation_fingerprints,
                inflight_operation_fingerprints=parent.inflight_operation_fingerprints,
                runtime_tool_registry=parent.runtime_tool_registry,
                metadata={**inherited_metadata, "subagent_id": agent_id},
            )

        all_tools = await self.tool_invoker.get_tool_definitions_filtered(invocation_context)
        allowed = defaults.get("allowed_tool_categories")
        if allowed is None:
            # Task agent: all tools except spawn_subagent (prevent recursion)
            tools = [t for t in all_tools if t.name != "spawn_subagent"]
        else:
            tools = [
                t
                for t in all_tools
                if t.name != "spawn_subagent"
                and t.category.value in allowed
                and (getattr(t, "capability_metadata", None) or {}).get("operation_kind") == "read"
            ]

        # The child gets an explicit, non-expanding allowlist even when the
        # legacy parent had ``None``. A fabricated model call therefore cannot
        # invoke a tool that was filtered out of the child catalog.
        parent_bindings = (
            invocation_context.capability_allowlist.bindings
            if invocation_context.capability_allowlist is not None
            else {}
        )
        names = frozenset(tool.name for tool in tools)
        invocation_context.capability_allowlist = CapabilityAllowlist(
            names,
            bindings={name: parent_bindings[name] for name in names if name in parent_bindings},
        )
        return tools, invocation_context

    def _pick_model(self, config: SubAgentConfig) -> str:
        """Select model by agent type — explore prefers fast models, others prefer strongest."""
        models = list(self.model_registry._models.values())
        if not models:
            return "qwen3.7-plus"
        if config.agent_type == SubAgentType.EXPLORE:
            for m in models:
                if "flash" in m.id.lower() or "turbo" in m.id.lower():
                    return m.id
        return models[0].id

    def _build_system_prompt(self, config: SubAgentConfig, defaults: dict) -> str:
        suffix = defaults.get("system_prompt_suffix", "")
        return f"""You are a specialized sub-agent within the AI Gateway platform.

{suffix}

Rules:
- Stay focused on the assigned task
- Be concise in your responses
- Report progress clearly
- If you encounter errors, report them and suggest alternatives
- Maximum turns: {config.max_turns}
"""

    @staticmethod
    def _side_effect_recovery(result: Any) -> dict[str, Any] | None:
        metadata = dict(getattr(result, "metadata", None) or {})
        tool_failure = metadata.get("tool_failure") or {}
        mcp_failure = metadata.get("mcp_failure") or {}
        unknown = str(getattr(result, "error", None) or "") in {
            "SIDE_EFFECT_UNKNOWN",
            "SIDE_EFFECT_UNRESOLVED",
        } or any(
            isinstance(value, dict)
            and (
                value.get("side_effect_state") == "unknown"
                or value.get("failure_kind") == "side_effect_unknown"
            )
            for value in (tool_failure, mcp_failure)
        )
        if not unknown:
            return None
        failure = mcp_failure if isinstance(mcp_failure, dict) and mcp_failure else tool_failure
        operation = metadata.get("mcp_operation") or metadata.get("tool_operation") or {}
        return {
            "recovery_action": str((failure or {}).get("recovery_action") or "pause"),
            "operation_id": str((operation or {}).get("operation_id") or ""),
            "read_back_available": bool((operation or {}).get("read_back_available")),
            "compensation_available": bool((operation or {}).get("compensation_available")),
            "failure": dict(failure or {}),
        }

    async def _run_loop(
        self,
        agent_id: str,
        state: SubAgentState,
        messages: list[dict[str, Any]],
        tools: list,
        model_id: str,
        system_prompt: str,
        config: SubAgentConfig,
        defaults: dict,
        kb_dataset_ids: list[str] | None = None,
        invocation_context: ToolInvocationContext | None = None,
        cancel_event: asyncio.Event | None = None,
        deadline: float | None = None,
        attempt_id: str = "",
        run_budget: RunBudget | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Simplified agent loop for sub-agents."""
        del defaults
        from ..tools.tool_registry import ToolCallResult

        tool_schemas = [t.to_openai_schema(compact=True) for t in tools] if tools else None
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + config.timeout_seconds

        for turn in range(config.max_turns):
            if asyncio.get_running_loop().time() > deadline:
                raise asyncio.TimeoutError()
            if cancel_event is not None and cancel_event.is_set():
                raise _ParentCancelled

            state.turns_completed = turn + 1
            yield {
                "event_type": "subagent_step",
                "data": {
                    "agent_id": agent_id,
                    "attempt_id": attempt_id,
                    "step": f"Turn {turn + 1}/{config.max_turns}",
                    "status": "running",
                },
            }

            full_text = ""
            tool_calls_accumulated: dict[str, dict] = {}
            tool_call_order: list[str] = []
            anon_counter = 0
            finish_reason: str | None = None

            try:
                if run_budget is not None:
                    run_budget.consume_model_turn()
                stream = self.model_registry.chat_stream(
                    model_id=model_id,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    tools=tool_schemas,
                    temperature=0.3,
                    max_tokens=config.max_tokens,
                )
                iterator = aiter(stream)
                while True:
                    try:
                        delta = await self._await_with_controls(
                            anext(iterator),
                            cancel_event=cancel_event,
                            deadline=deadline,
                        )
                    except StopAsyncIteration:
                        break
                    if delta.content:
                        full_text += delta.content
                        yield {
                            "event_type": "subagent_text_delta",
                            "data": {
                                "agent_id": agent_id,
                                "attempt_id": attempt_id,
                                "text": delta.content,
                            },
                        }
                    if delta.tool_calls:
                        anon_counter = merge_stream_tool_calls(
                            delta.tool_calls,
                            tool_calls_accumulated,
                            tool_call_order,
                            anon_counter,
                        )
                    delta_finish_reason = getattr(delta, "finish_reason", None)
                    if delta_finish_reason is not None:
                        finish_reason = str(delta_finish_reason).strip().lower()
            except (RunBudgetExceeded, _ParentCancelled, asyncio.TimeoutError):
                raise
            except Exception as e:
                logger.error(
                    "SubAgent %s LLM call failed (exception_type=%s)",
                    agent_id,
                    type(e).__name__,
                )
                state.error = str(e)
                raise

            tool_calls = [tool_calls_accumulated[k] for k in tool_call_order]
            allowed_finish_reasons = (
                {"stop", "tool_calls", "function_call", "tool_use"}
                if tool_calls
                else {"stop", "end_turn", "stop_sequence"}
            )
            if finish_reason is not None and finish_reason not in allowed_finish_reasons:
                state.error = f"Sub-agent model stopped before completion ({finish_reason})"
                return

            if not tool_calls:
                messages.append({"role": "assistant", "content": full_text})
                return

            messages.append({"role": "assistant", "content": full_text, "tool_calls": tool_calls})

            for tc in tool_calls:
                if state.tool_calls_made >= config.max_tool_calls:
                    state.error = "Sub-agent tool-call budget exhausted"
                    return

                state.tool_calls_made += 1
                if run_budget is not None:
                    run_budget.reserve_tool_batch(1)
                fn = tc.get("function", {})
                tool_name = fn.get("name", "unknown")
                call_id = tc.get("id", f"call_{turn}")

                invalid_arguments: str | None = None
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                    if not isinstance(tool_args, dict):
                        invalid_arguments = "Tool arguments must be a JSON object"
                        tool_args = {}
                except (json.JSONDecodeError, TypeError):
                    invalid_arguments = "Tool arguments must be valid JSON"
                    tool_args = {}

                # Auto-inject kb_dataset_ids for KB search (inherited from parent)
                if (
                    invalid_arguments is None
                    and tool_name == "search_knowledge_base"
                    and kb_dataset_ids
                    and not tool_args.get("dataset_ids")
                ):
                    tool_args["dataset_ids"] = kb_dataset_ids

                yield {
                    "event_type": "subagent_tool_start",
                    "data": {
                        "agent_id": agent_id,
                        "attempt_id": attempt_id,
                        "tool_name": tool_name,
                        "call_id": call_id,
                    },
                }

                start = time.time()
                try:
                    if invalid_arguments is not None:
                        result = ToolCallResult(
                            call_id=call_id,
                            tool_name=tool_name,
                            success=False,
                            error=invalid_arguments,
                        )
                    elif invocation_context is None:
                        raise RuntimeError("Sub-agent tool context is unavailable")
                    elif self.execution_gateway and self.execution_gateway.enabled:
                        result = await self._await_with_controls(
                            self.execution_gateway.invoke_tool(
                                tool_name=tool_name,
                                arguments=tool_args,
                                context=invocation_context,
                                cancel_event=cancel_event,
                            ),
                            cancel_event=cancel_event,
                            deadline=deadline,
                        )
                    else:
                        result = await self._await_with_controls(
                            self.tool_invoker.invoke(
                                tool_name=tool_name,
                                arguments=tool_args,
                                context=invocation_context,
                                cancel_event=cancel_event,
                            ),
                            cancel_event=cancel_event,
                            deadline=deadline,
                        )
                except (RunBudgetExceeded, _ParentCancelled, asyncio.TimeoutError):
                    raise
                except Exception as e:
                    result = ToolCallResult(
                        call_id=call_id,
                        tool_name=tool_name,
                        success=False,
                        error=str(e),
                    )
                duration = (time.time() - start) * 1000

                result_str = (
                    str(result.result or "")[:2000] if result.success else (result.error or "Error")
                )
                if run_budget is not None:
                    run_budget.observe_tool_result(result_str)

                yield {
                    "event_type": "subagent_tool_result",
                    "data": {
                        "agent_id": agent_id,
                        "attempt_id": attempt_id,
                        "tool_name": tool_name,
                        "call_id": call_id,
                        "success": result.success,
                        "duration_ms": round(duration, 1),
                        "summary": result_str[:200],
                    },
                }

                state.steps.append(
                    SubAgentStep(
                        tool_name=tool_name,
                        call_id=call_id,
                        status="completed" if result.success else "failed",
                        summary=result_str[:200],
                        duration_ms=duration,
                    )
                )

                recovery = self._side_effect_recovery(result)
                if recovery is not None:
                    state.status = "blocked"
                    state.error = "SIDE_EFFECT_UNKNOWN"
                    yield {
                        "event_type": "subagent_side_effect_unknown",
                        "data": {
                            "agent_id": agent_id,
                            "attempt_id": attempt_id,
                            "tool_name": tool_name,
                            "call_id": call_id,
                            "status": "blocked",
                            "error": state.error,
                            **recovery,
                        },
                    }
                    return

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_str,
                    }
                )
        state.error = "Sub-agent turn budget exhausted"

    @staticmethod
    def _summarize(text: str, max_length: int = 2000) -> str:
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
