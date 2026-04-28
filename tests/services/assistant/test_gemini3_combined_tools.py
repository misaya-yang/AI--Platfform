"""Regression test for Gemini 3.x combined tool support.

Gemini 3 explicitly supports combining built-in grounding (``google_search``)
with user-defined ``functionDeclarations`` in a single request — see
https://ai.google.dev/gemini-api/docs/gemini-3 ("Combining built-in tools
with function calling is now supported for Gemini 3 models").

The legacy 1.5 / 2.0 API used to 400 on the combo, so an old defensive
branch in ``_build_google_body`` silently dropped the search tool whenever
``functionDeclarations`` were also in scope. We've now retired the 1.5/2.0
catalog and only ship Gemini 3.x; this test pins the new behaviour:

  - Pass BOTH a function tool AND ``native_search_config={"tool_type":
    "google_search"}``.
  - Assert the resulting body's ``tools`` array contains BOTH a
    ``functionDeclarations`` entry AND a ``google_search`` entry.
"""

from __future__ import annotations

import pytest

from assistant_service.core.models.model_registry import (
    ChatMessage,
    ModelProvider,
    ModelRegistry,
)


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry(use_default_models=False)


def _function_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Look up internal documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    }


def test_gemini3_keeps_google_search_alongside_function_declarations(
    registry: ModelRegistry,
):
    """Gemini 3 must NOT drop ``google_search`` when functionDeclarations
    are present — both must coexist in the outbound ``tools`` array."""
    body = registry._build_request_body(
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3-pro-preview",
        messages=[ChatMessage(role="user", content="What's the weather today?")],
        temperature=0.7,
        max_tokens=None,
        tools=[_function_tool()],
        stream=False,
        native_search_config={"tool_type": "google_search"},
    )

    tools = body.get("tools") or []
    # Must have at least two entries: one with functionDeclarations, one
    # with google_search. Gemini accepts multiple tool objects per request.
    assert any("functionDeclarations" in t for t in tools), (
        f"Expected functionDeclarations entry in tools array, got: {tools}"
    )
    assert any("google_search" in t for t in tools), (
        f"Expected google_search entry in tools array, got: {tools}"
    )

    # And the function tool must still carry the original schema.
    fn_entries = [t for t in tools if "functionDeclarations" in t]
    assert len(fn_entries) == 1
    decls = fn_entries[0]["functionDeclarations"]
    assert any(d.get("name") == "search_knowledge_base" for d in decls)


def test_gemini3_vertex_keeps_google_search_alongside_function_declarations(
    registry: ModelRegistry,
):
    """Same contract for the GOOGLE_VERTEX provider variant — Vertex shares
    the Gemini wire format and the same combined-tools support."""
    body = registry._build_request_body(
        provider=ModelProvider.GOOGLE_VERTEX,
        model_id="gemini-3-flash-preview",
        messages=[ChatMessage(role="user", content="latest AI news?")],
        temperature=0.7,
        max_tokens=None,
        tools=[_function_tool()],
        stream=False,
        native_search_config={"tool_type": "google_search"},
    )

    tools = body.get("tools") or []
    assert any("functionDeclarations" in t for t in tools)
    assert any("google_search" in t for t in tools)


def test_gemini_legacy_search_retrieval_also_coexists(registry: ModelRegistry):
    """If a model still advertises the legacy ``google_search_retrieval`` form,
    the same coexistence rule applies — append, do not drop."""
    body = registry._build_request_body(
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3-pro-preview",
        messages=[ChatMessage(role="user", content="hi")],
        tools=[_function_tool()],
        native_search_config={"tool_type": "google_search_retrieval"},
    )
    tools = body.get("tools") or []
    assert any("functionDeclarations" in t for t in tools)
    assert any("google_search_retrieval" in t for t in tools)


def test_gemini_native_search_alone_still_works(registry: ModelRegistry):
    """Sanity: when no function tools are passed, the search tool still
    lands in the outbound body (no regression on the no-functions path)."""
    body = registry._build_request_body(
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3-pro-preview",
        messages=[ChatMessage(role="user", content="hi")],
        tools=None,
        native_search_config={"tool_type": "google_search"},
    )
    tools = body.get("tools") or []
    assert any("google_search" in t for t in tools)
    assert not any("functionDeclarations" in t for t in tools)
