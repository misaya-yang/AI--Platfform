"""
Sub-Agent Manager — orchestrates explore/task/plan sub-agents.

ADR-003: Creates isolated sub-agents with their own context windows,
runs them to completion, and returns summarized results to the main agent.
SSE events are yielded for real-time frontend display.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator

from ....core.observability.logging import get_logger
from .stream_helpers import merge_stream_tool_calls
from .subagent_types import (
    SUBAGENT_DEFAULTS,
    SubAgentConfig,
    SubAgentState,
    SubAgentStep,
    SubAgentType,
)

if TYPE_CHECKING:
    from ..models.model_registry import ModelRegistry
    from ..tools.tool_registry import ToolCallRequest, ToolCallResult, ToolRegistry

logger = get_logger(__name__)


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
    ) -> None:
        self.model_registry = model_registry
        self.tool_registry = tool_registry
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

        yield {
            "event_type": "subagent_started",
            "data": {
                "agent_id": agent_id,
                "agent_type": config.agent_type.value,
                "description": state.description,
                "prompt": config.prompt[:200],
            },
        }

        try:
            messages = self._build_messages(config)
            tools = self._get_tools(config, defaults, parent_user)
            model_id = config.model_override or self._pick_model(config)
            system_prompt = self._build_system_prompt(config, defaults)

            result_text = ""
            async for event in self._run_loop(
                agent_id, state, messages, tools, model_id, system_prompt, config, defaults,
                kb_dataset_ids=kb_dataset_ids,
            ):
                yield event
                if event["event_type"] == "subagent_text_delta":
                    result_text += event["data"].get("text", "")

            state.status = "completed"
            state.result = result_text
            state.finished_at = time.time()
            state.duration_ms = (state.finished_at - (state.started_at or 0)) * 1000

            yield {
                "event_type": "subagent_finished",
                "data": {
                    "agent_id": agent_id,
                    "status": "completed",
                    "result_summary": self._summarize(result_text),
                    "duration_ms": state.duration_ms,
                    "turns": state.turns_completed,
                    "tool_calls": state.tool_calls_made,
                },
            }

        except asyncio.TimeoutError:
            state.status = "failed"
            state.error = f"Timeout after {config.timeout_seconds}s"
            yield {"event_type": "subagent_finished", "data": {"agent_id": agent_id, "status": "failed", "error": state.error}}
        except Exception as e:
            state.status = "failed"
            state.error = str(e)
            logger.error(f"SubAgent {agent_id} failed: {e}")
            yield {"event_type": "subagent_finished", "data": {"agent_id": agent_id, "status": "failed", "error": str(e)}}
        finally:
            self._active.pop(agent_id, None)

    async def spawn_parallel(
        self,
        configs: list[SubAgentConfig],
        parent_user: Any | None = None,
        parent_tenant_id: str = "",
        kb_dataset_ids: list[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Spawn multiple sub-agents in parallel. Merges event streams."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        done_count = 0
        total = len(configs)

        async def _run(cfg: SubAgentConfig) -> None:
            async for event in self.spawn(cfg, parent_user, parent_tenant_id, kb_dataset_ids=kb_dataset_ids):
                await queue.put(event)

        tasks = [asyncio.create_task(_run(c)) for c in configs]

        while done_count < total:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                yield event
                if event["event_type"] == "subagent_finished":
                    done_count += 1
            except asyncio.TimeoutError:
                for t in tasks:
                    if t.done() and t.exception():
                        done_count += 1

        for t in tasks:
            if not t.done():
                t.cancel()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_messages(self, config: SubAgentConfig) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if config.parent_context:
            messages.append({"role": "user", "content": f"Context from parent agent:\n{config.parent_context}"})
        messages.append({"role": "user", "content": config.prompt})
        return messages

    def _get_tools(self, config: SubAgentConfig, defaults: dict, user: Any | None) -> list:
        all_tools = self.tool_registry.list_tools(user=user)
        allowed = defaults.get("allowed_tool_categories")
        if allowed is None:
            # Task agent: all tools except spawn_subagent (prevent recursion)
            return [t for t in all_tools if t.name != "spawn_subagent"]
        return [t for t in all_tools if t.category.value in allowed]

    def _pick_model(self, config: SubAgentConfig) -> str:
        """Select model by agent type — explore prefers fast models, others prefer strongest."""
        models = list(self.model_registry._models.values())
        if not models:
            return "qwen3.6-plus"
        if config.agent_type == SubAgentType.EXPLORE:
            for m in models:
                if "flash" in m.id.lower() or "turbo" in m.id.lower():
                    return m.id
        return models[0].id

    def _build_system_prompt(self, config: SubAgentConfig, defaults: dict) -> str:
        suffix = defaults.get("system_prompt_suffix", "")
        return f"""You are a specialized sub-agent within the Hejaz AI platform.

{suffix}

Rules:
- Stay focused on the assigned task
- Be concise in your responses
- Report progress clearly
- If you encounter errors, report them and suggest alternatives
- Maximum turns: {config.max_turns}
"""

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
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Simplified agent loop for sub-agents."""
        from ..tools.tool_registry import ToolCallRequest, ToolCallResult

        tool_schemas = [t.to_openai_schema(compact=True) for t in tools] if tools else None

        deadline = time.time() + config.timeout_seconds

        for turn in range(config.max_turns):
            if time.time() > deadline:
                raise asyncio.TimeoutError()

            state.turns_completed = turn + 1
            yield {
                "event_type": "subagent_step",
                "data": {"agent_id": agent_id, "step": f"Turn {turn + 1}/{config.max_turns}", "status": "running"},
            }

            full_text = ""
            tool_calls_accumulated: dict[str, dict] = {}
            tool_call_order: list[str] = []
            anon_counter = 0

            try:
                async for delta in self.model_registry.chat_stream(
                    model_id=model_id,
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    tools=tool_schemas,
                    temperature=0.3,
                ):
                    if delta.content:
                        full_text += delta.content
                        yield {
                            "event_type": "subagent_text_delta",
                            "data": {"agent_id": agent_id, "text": delta.content},
                        }
                    if delta.tool_calls:
                        anon_counter = merge_stream_tool_calls(
                            delta.tool_calls, tool_calls_accumulated, tool_call_order, anon_counter,
                        )
            except Exception as e:
                logger.error(f"SubAgent {agent_id} LLM call failed: {e}")
                state.error = str(e)
                return

            tool_calls = [tool_calls_accumulated[k] for k in tool_call_order]

            if not tool_calls:
                messages.append({"role": "assistant", "content": full_text})
                break

            messages.append({"role": "assistant", "content": full_text, "tool_calls": tool_calls})

            for tc in tool_calls:
                if state.tool_calls_made >= config.max_tool_calls:
                    break

                state.tool_calls_made += 1
                fn = tc.get("function", {})
                tool_name = fn.get("name", "unknown")
                call_id = tc.get("id", f"call_{turn}")

                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                # Auto-inject kb_dataset_ids for KB search (inherited from parent)
                if tool_name == "search_knowledge_base" and kb_dataset_ids and not tool_args.get("dataset_ids"):
                    tool_args["dataset_ids"] = kb_dataset_ids

                yield {
                    "event_type": "subagent_tool_start",
                    "data": {"agent_id": agent_id, "tool_name": tool_name, "call_id": call_id},
                }

                start = time.time()
                try:
                    request = ToolCallRequest(
                        call_id=call_id,
                        tool_name=tool_name,
                        arguments=tool_args,
                    )
                    result = await self.tool_registry.execute(request)
                except Exception as e:
                    result = ToolCallResult(
                        call_id=call_id, tool_name=tool_name, success=False, error=str(e),
                    )
                duration = (time.time() - start) * 1000

                result_str = str(result.result or "")[:2000] if result.success else (result.error or "Error")

                yield {
                    "event_type": "subagent_tool_result",
                    "data": {
                        "agent_id": agent_id,
                        "tool_name": tool_name,
                        "call_id": call_id,
                        "success": result.success,
                        "duration_ms": round(duration, 1),
                        "summary": result_str[:200],
                    },
                }

                state.steps.append(SubAgentStep(
                    tool_name=tool_name,
                    call_id=call_id,
                    status="completed" if result.success else "failed",
                    summary=result_str[:200],
                    duration_ms=duration,
                ))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_str,
                })

    @staticmethod
    def _summarize(text: str, max_length: int = 2000) -> str:
        if len(text) <= max_length:
            return text
        return text[:max_length] + "..."
