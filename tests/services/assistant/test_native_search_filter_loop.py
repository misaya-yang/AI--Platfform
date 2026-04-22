"""Regression tests: the agent loop must strip `search_web` from the tool
list passed to the provider whenever the selected model is registered as
native-search-capable.

Why this exists
---------------
Production incident: Qwen 3.6 Plus was correctly flagged as native-search
capable in the capability map (``NATIVE_SEARCH_CAPABLE``), and the
``enable_search: true`` body field was being injected — but the loop was
STILL forwarding the Tavily-backed ``search_web`` function schema to the
provider. The model, given both options, sometimes called the Tavily
tool instead of using the built-in search, producing worse grounding and
an extra billed Tavily round-trip.

The existing unit tests in ``test_native_web_search.py`` covered:
  - capability flag auto-population on ``ModelInfo``
  - ``_build_openai_body`` / ``_build_google_body`` shaping
  - a standalone list-filter helper (not the loop path)

They did NOT exercise ``_execute_streaming_first`` end-to-end, so a
regression could re-introduce the bug without a failing test. This file
closes that gap by driving the real loop with a fake ModelRegistry that
captures the ``tools=`` and ``native_search_config=`` kwargs on the
single ``chat_stream`` call and asserts the filter behavior.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("tiktoken", reason="tiktoken required for model registry tests")


# --------------------------------------------------------------------------- #
# Minimal fakes — copied in style from test_agentloop_streaming_first_contract
# (kept local so this file is self-contained and fast).
# --------------------------------------------------------------------------- #


@dataclass
class MockUserContext:
    user_id: str
    tenant_id: str = "tenant1"
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] | None = None

    def __post_init__(self) -> None:
        if self.roles is None:
            self.roles = []


class FakeModelInfo:
    """Stand-in for ``ModelInfo`` — exposes only the attributes the loop reads.

    The loop filter at ``agent_loop.py:1926-1943`` uses
    ``getattr(_model_info, "supports_native_search", False)`` and then
    ``getattr(_model_info, "native_search_config", None)``, so we only need
    those two plus ``supports_vision`` (read elsewhere in the loop).
    """

    def __init__(
        self,
        *,
        supports_native_search: bool = False,
        native_search_config: dict[str, Any] | None = None,
        provider: str | None = None,
    ) -> None:
        self.supports_vision = False
        self.supports_native_search = supports_native_search
        self.native_search_config = native_search_config
        # Only set when a test needs to exercise provider-aware branches
        # (e.g. Gemini's google_search incompatibility with function tools).
        if provider is not None:
            self.provider = provider


class CapturingModelRegistry:
    """ModelRegistry stub that records every ``chat_stream`` call.

    Yields a single text delta per call so the loop terminates after one
    iteration (no tool calls => ``tool_calls_batch`` empty => break).
    """

    def __init__(self, *, model_infos: dict[str, FakeModelInfo]) -> None:
        self._model_infos = model_infos
        self.calls: list[dict[str, Any]] = []

    # Mirrors ModelRegistry.get_model — returns None for unknown ids, which
    # matches production behavior and exercises the
    # ``if _model_info and getattr(...)`` short-circuit on the unknown path.
    def get_model(self, model_id: str) -> FakeModelInfo | None:
        return self._model_infos.get(model_id)

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        from assistant_service.core.models.model_registry import StreamDelta

        # Capture everything the loop passes — we assert on ``tools`` and
        # ``native_search_config`` below.
        self.calls.append(
            {
                "model_id": kwargs.get("model_id"),
                "tools": kwargs.get("tools"),
                "native_search_config": kwargs.get("native_search_config"),
                "messages": kwargs.get("messages"),
                "temperature": kwargs.get("temperature"),
                "max_tokens": kwargs.get("max_tokens"),
                "thinking_level": kwargs.get("thinking_level"),
            }
        )
        # Emit one content delta, no tool_calls, so the loop breaks.
        yield StreamDelta(content="ok", tool_calls=None, usage={"input_tokens": 1, "output_tokens": 1})


class FakeToolDef:
    """Minimal ToolDefinition stand-in accepted by select_tools and the loop."""

    def __init__(self, name: str):
        self.name = name
        self.category = None
        self.description = f"{name} tool"
        # select_tools reads this to score relevance; empty keeps scoring
        # deterministic and lets the name-token fallback win.
        self.relevance_keywords: list[str] = []

    def to_openai_schema(self, compact: bool = True) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }


class FakeToolInvoker:
    """Exposes ``search_web`` (and optionally others) to the loop.

    The loop calls ``get_tool_definitions_filtered`` once to assemble the
    schema list; after that the list flows through ``select_tools`` and
    lands at the native-search filter.
    """

    def __init__(self, tool_names: list[str]):
        self._tool_names = list(tool_names)

    def get_tool_definitions(
        self, context: Any, tool_names: list[str] | None = None
    ) -> list[FakeToolDef]:
        names = self._tool_names if tool_names is None else [n for n in self._tool_names if n in tool_names]
        return [FakeToolDef(n) for n in names]

    async def get_tool_definitions_filtered(
        self, context: Any, tool_names: list[str] | None = None
    ) -> list[FakeToolDef]:
        return self.get_tool_definitions(context, tool_names)


def _tool_names_in(schemas: list[dict[str, Any]] | None) -> list[str]:
    """Pull the function names out of the captured OpenAI-style schema list."""
    if not schemas:
        return []
    names: list[str] = []
    for s in schemas:
        fn = (s or {}).get("function") or {}
        name = fn.get("name") or s.get("name")
        if name:
            names.append(str(name))
    return names


def _build_loop(
    *,
    model_infos: dict[str, FakeModelInfo],
    tool_names: list[str],
) -> tuple[Any, CapturingModelRegistry]:
    from assistant_service.core.agent.agent_loop import AgentLoop

    registry = CapturingModelRegistry(model_infos=model_infos)
    invoker = FakeToolInvoker(tool_names=tool_names)
    loop = AgentLoop(
        model_registry=registry,
        tool_invoker=invoker,  # type: ignore[arg-type]
    )
    return loop, registry


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_native_capable_model_drops_search_web_from_tools() -> None:
    """
    Given a model registered as native-search-capable (qwen3.6-plus) AND a
    tool list containing ``search_web``, the loop must call
    ``chat_stream(tools=...)`` WITHOUT ``search_web`` in the schema list.

    This is the exact regression: Qwen + Tavily leaking through.
    """
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    loop, registry = _build_loop(
        model_infos={
            "qwen3.6-plus": FakeModelInfo(
                supports_native_search=True,
                native_search_config={"enable_search": True},
            ),
        },
        tool_names=["search_web", "search_knowledge_base"],
    )

    cfg = AgentLoopConfig(model_id="qwen3.6-plus", max_tool_iterations=1)
    user = MockUserContext(user_id="u1")

    # Use a message that scores search_web highly so it survives the
    # relevance-based selector and reaches the native-search filter.
    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="what is the latest news about the cavaliers",
        config=cfg,
        history=[],
    ):
        pass

    assert len(registry.calls) == 1, f"expected 1 chat_stream call, got {len(registry.calls)}"
    call = registry.calls[0]
    forwarded = _tool_names_in(call["tools"])
    assert "search_web" not in forwarded, (
        f"search_web leaked through the native-search filter for qwen3.6-plus; "
        f"tools forwarded to provider: {forwarded}"
    )


@pytest.mark.asyncio
async def test_non_native_model_keeps_search_web_in_tools() -> None:
    """
    Given a model that is NOT native-search-capable (deepseek-chat), the
    loop must pass ``search_web`` through to the provider so the Tavily
    fallback path still works.

    This is the negative case — ensures the filter isn't over-aggressive.
    """
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    loop, registry = _build_loop(
        model_infos={
            "deepseek-chat": FakeModelInfo(
                supports_native_search=False,
                native_search_config=None,
            ),
        },
        tool_names=["search_web", "search_knowledge_base"],
    )

    cfg = AgentLoopConfig(model_id="deepseek-chat", max_tool_iterations=1)
    user = MockUserContext(user_id="u1")

    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="what is the latest news about the cavaliers",
        config=cfg,
        history=[],
    ):
        pass

    assert len(registry.calls) == 1
    call = registry.calls[0]
    forwarded = _tool_names_in(call["tools"])
    assert "search_web" in forwarded, (
        f"search_web was unexpectedly stripped for deepseek-chat (no native "
        f"search); tools forwarded: {forwarded}"
    )
    # native_search_config must be None for non-capable models — otherwise
    # the OpenAI body builder would inject enable_search.
    assert call["native_search_config"] is None


@pytest.mark.asyncio
async def test_native_search_config_passthrough_for_capable_model() -> None:
    """
    The provider-specific ``native_search_config`` must flow from the
    capability map all the way to the ``chat_stream`` kwarg, so the body
    builder can inject ``enable_search: true`` (or the provider equivalent).

    Regression guard: if someone refactors the filter and forgets to keep
    ``native_search_cfg`` wired through, the body builder would silently
    skip injection and the bug becomes asymptomatic — no search_web tool,
    but also no enable_search flag. This asserts the full wiring.
    """
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    loop, registry = _build_loop(
        model_infos={
            "qwen3.6-plus": FakeModelInfo(
                supports_native_search=True,
                native_search_config={"enable_search": True},
            ),
        },
        tool_names=["search_web"],
    )

    cfg = AgentLoopConfig(model_id="qwen3.6-plus", max_tool_iterations=1)
    user = MockUserContext(user_id="u1")

    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="latest news please",
        config=cfg,
        history=[],
    ):
        pass

    assert len(registry.calls) == 1
    call = registry.calls[0]
    assert call["native_search_config"] == {"enable_search": True}, (
        f"native_search_config failed to pass through to chat_stream: "
        f"got {call['native_search_config']!r}"
    )


@pytest.mark.asyncio
async def test_native_capable_model_preserves_other_tools() -> None:
    """
    The filter must be surgical: only ``search_web`` is removed. Other
    tools (KB search, code exec, etc.) must still reach the provider.

    This guards against a regression where someone nukes the whole tool
    list instead of filtering by name.
    """
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    loop, registry = _build_loop(
        model_infos={
            "qwen3.6-plus": FakeModelInfo(
                supports_native_search=True,
                native_search_config={"enable_search": True},
            ),
        },
        tool_names=["search_web", "search_knowledge_base", "update_memory"],
    )

    cfg = AgentLoopConfig(model_id="qwen3.6-plus", max_tool_iterations=1)
    user = MockUserContext(user_id="u1")

    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="what is the latest news about the knowledge base and memory",
        config=cfg,
        history=[],
    ):
        pass

    assert len(registry.calls) == 1
    forwarded = _tool_names_in(registry.calls[0]["tools"])
    assert "search_web" not in forwarded
    # At least one non-web tool should still be present — otherwise the
    # filter is over-reaching. KB and memory are TIER_ALWAYS so they're
    # guaranteed to survive relevance scoring.
    assert "search_knowledge_base" in forwarded, forwarded
    assert "update_memory" in forwarded, forwarded


# --------------------------------------------------------------------------- #
# Gemini regression — google_search can NOT coexist with functionDeclarations.
# Live incident 2026-04-21: switching to Gemini 3 Flash produced a 400 from
# the Gemini REST endpoint (~850ms, 0 steps in the Activity drawer) because
# the loop was dropping `search_web` and forwarding `native_search_config`
# with the full tool list. This suite locks in the compat behavior: for
# Google providers with function tools present, native search is suppressed
# and `search_web` stays in scope.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gemini_keeps_search_web_and_suppresses_native() -> None:
    """
    Gemini → `search_web` MUST NOT be dropped and `native_search_config`
    MUST be None, because `_build_gemini_body` would otherwise append
    `{google_search: {}}` alongside functionDeclarations and the API
    returns 400. The assistant always has function tools in scope, so the
    policy is to unconditionally suppress native-search for Google provider.
    """
    from assistant_service.core.agent.agent_loop import AgentLoopConfig

    loop, registry = _build_loop(
        model_infos={
            "gemini-3-flash-preview": FakeModelInfo(
                supports_native_search=True,
                native_search_config={"tool_type": "google_search"},
                provider="google",
            ),
        },
        tool_names=["search_web", "search_knowledge_base"],
    )

    cfg = AgentLoopConfig(
        model_id="gemini-3-flash-preview", max_tool_iterations=1
    )
    user = MockUserContext(user_id="u1")

    async for _ in loop.execute(
        session_id="s1",
        user=user,  # type: ignore[arg-type]
        message="帮我做一个quiz的关于transformer的测验题",
        config=cfg,
        history=[],
    ):
        pass

    assert len(registry.calls) == 1
    call = registry.calls[0]
    forwarded = _tool_names_in(call["tools"])
    assert "search_web" in forwarded, (
        f"search_web was stripped for Gemini — will produce an invalid "
        f"request because google_search cannot coexist with functionDeclarations; "
        f"tools forwarded: {forwarded}"
    )
    assert call["native_search_config"] is None, (
        f"native_search_config leaked to Gemini while function tools were "
        f"present; would trigger a 400 from the API. Got: "
        f"{call['native_search_config']!r}"
    )
