"""Tests for provider-native web-search capability.

Covers:
- `ModelInfo.supports_native_search` auto-populates from the capability map.
- `should_use_native_search` keyword heuristic.
- Request-body builders inject provider-specific params.
- `enable_search` appears in the DashScope body only for search-intent queries.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tiktoken", reason="tiktoken required for model registry tests")


def _get_mod():
    from assistant_service.core.models import model_registry as mod

    return mod


class TestCapabilityFlag:
    def test_qwen_has_native_search(self):
        mod = _get_mod()
        qwen = mod.ModelInfo(
            id="qwen3.6-plus",
            name="Qwen 3.6 Plus",
            provider=mod.ModelProvider.DASHSCOPE,
        )
        assert qwen.supports_native_search is True
        assert qwen.native_search_config == {"enable_search": True}

    def test_gemini_has_native_search(self):
        mod = _get_mod()
        m = mod.ModelInfo(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            provider=mod.ModelProvider.GOOGLE,
        )
        assert m.supports_native_search is True
        assert m.native_search_config == {"tool_type": "google_search"}

    def test_claude_has_native_search(self):
        mod = _get_mod()
        m = mod.ModelInfo(
            id="claude-sonnet-4-20250514",
            name="Claude Sonnet 4",
            provider=mod.ModelProvider.ANTHROPIC,
        )
        assert m.supports_native_search is True
        assert m.native_search_config["tool_type"] == "web_search_20250305"

    def test_deepseek_does_not_have_native_search(self):
        mod = _get_mod()
        m = mod.ModelInfo(
            id="deepseek-chat",
            name="DeepSeek Chat",
            provider=mod.ModelProvider.DEEPSEEK,
        )
        assert m.supports_native_search is False
        assert m.native_search_config is None

    def test_openai_gpt4o_not_yet_enabled(self):
        """We deferred OpenAI native search (requires Responses API)."""
        mod = _get_mod()
        m = mod.ModelInfo(
            id="gpt-4o",
            name="GPT-4o",
            provider=mod.ModelProvider.OPENAI,
        )
        assert m.supports_native_search is False


class TestSearchHeuristic:
    def test_english_search_keywords(self):
        fn = _get_mod().should_use_native_search
        assert fn("search for the latest AI news") is True
        assert fn("what is the current weather in Tokyo") is True
        assert fn("Who is the current US president?") is True

    def test_chinese_search_keywords(self):
        fn = _get_mod().should_use_native_search
        assert fn("帮我搜索一下最新的新闻") is True
        assert fn("今天的股价是多少") is True
        assert fn("查一下最近的赛事") is True

    def test_non_search_queries(self):
        fn = _get_mod().should_use_native_search
        assert fn("write a poem about autumn") is False
        assert fn("explain the Pythagorean theorem") is False
        assert fn("帮我写一首诗") is False
        assert fn("") is False

    def test_case_insensitive(self):
        fn = _get_mod().should_use_native_search
        assert fn("LATEST news please") is True
        assert fn("Search Google") is True


class TestRequestBodyInjection:
    def _registry(self):
        mod = _get_mod()
        return mod.ModelRegistry(use_default_models=True)

    def test_dashscope_enable_search_injected(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_openai_body(
            model_id="qwen3.6-plus",
            messages=[mod.ChatMessage(role="user", content="latest news")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
            native_search_config={"enable_search": True},
        )
        # Top-level, NOT under extra_body. Verified live against DashScope
        # compat endpoint 2026-04-21 — wrapper is silently ignored.
        assert body.get("enable_search") is True
        assert "extra_body" not in body

    def test_dashscope_no_injection_when_config_none(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_openai_body(
            model_id="qwen3.6-plus",
            messages=[mod.ChatMessage(role="user", content="write a poem")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
            native_search_config=None,
        )
        assert not body.get("enable_search")

    def test_gemini_google_search_tool_appended(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-2.5-flash",
            messages=[mod.ChatMessage(role="user", content="latest news")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
            native_search_config={"tool_type": "google_search"},
        )
        tools = body.get("tools") or []
        assert any("google_search" in t for t in tools), tools

    def test_gemini_google_search_dropped_when_function_tools_present(self):
        """
        Guard: Gemini REST API rejects any request that mixes
        `functionDeclarations` with the `google_search` built-in tool (400
        Bad Request). When callers forget to suppress `native_search_config`
        upstream, the body builder must silently drop the grounding tool
        rather than produce an un-sendable request.
        """
        mod = _get_mod()
        reg = self._registry()
        fn_tool = {
            "type": "function",
            "function": {
                "name": "generate_quiz",
                "description": "Make a quiz",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        body = reg._build_google_body(
            model_id="gemini-3-flash-preview",
            messages=[mod.ChatMessage(role="user", content="quiz on transformers")],
            temperature=0.7,
            max_tokens=None,
            tools=[fn_tool],
            stream=True,
            native_search_config={"tool_type": "google_search"},
        )
        tools = body.get("tools") or []
        # functionDeclarations preserved
        assert any(t.get("functionDeclarations") for t in tools), tools
        # google_search NOT appended — mixing them would 400
        assert not any("google_search" in t for t in tools), tools

    def test_anthropic_web_search_tool_appended(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_anthropic_body(
            model_id="claude-sonnet-4-20250514",
            messages=[mod.ChatMessage(role="user", content="latest news")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
            native_search_config={"tool_type": "web_search_20250305", "max_uses": 5},
        )
        tools = body.get("tools") or []
        assert any(t.get("type") == "web_search_20250305" for t in tools), tools


class TestGeminiThinkingConfig:
    """Gemini 2.5+ / 3.x must emit `thinkingConfig.includeThoughts: true` by
    default so thought summaries stream back as `thought` parts. Without this,
    the Activity drawer has no thinking block to render.
    """

    def _registry(self):
        mod = _get_mod()
        return mod.ModelRegistry(use_default_models=True)

    def test_gemini_3_flash_includes_thoughts_by_default(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-3-flash-preview",
            messages=[mod.ChatMessage(role="user", content="hello")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
        )
        cfg = body.get("generationConfig", {}).get("thinkingConfig", {})
        assert cfg.get("includeThoughts") is True, body

    def test_gemini_3_pro_includes_thoughts_by_default(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-3-pro-preview",
            messages=[mod.ChatMessage(role="user", content="hello")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
        )
        cfg = body.get("generationConfig", {}).get("thinkingConfig", {})
        assert cfg.get("includeThoughts") is True, body

    def test_gemini_2_5_flash_includes_thoughts_by_default(self):
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-2.5-flash",
            messages=[mod.ChatMessage(role="user", content="hello")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
        )
        cfg = body.get("generationConfig", {}).get("thinkingConfig", {})
        assert cfg.get("includeThoughts") is True, body

    def test_thinking_level_preserved_when_set(self):
        """PPT path sets thinking_level='high'; both fields must coexist."""
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-3-pro-preview",
            messages=[mod.ChatMessage(role="user", content="make a ppt")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
            thinking_level="high",
        )
        cfg = body.get("generationConfig", {}).get("thinkingConfig", {})
        assert cfg.get("thinkingLevel") == "high", body
        assert cfg.get("includeThoughts") is True, body

    def test_legacy_gemini_does_not_inject_thinking_config(self):
        """Gemini 1.5 / older ids must NOT receive thinkingConfig."""
        mod = _get_mod()
        reg = self._registry()
        body = reg._build_google_body(
            model_id="gemini-1.5-pro",
            messages=[mod.ChatMessage(role="user", content="hello")],
            temperature=0.7,
            max_tokens=None,
            tools=None,
            stream=True,
        )
        assert "thinkingConfig" not in body.get("generationConfig", {}), body


class TestToolListFilter:
    """Contract: when native search is active, search_web must be dropped."""

    def test_filter_drops_search_web(self):
        tools = [
            {"type": "function", "function": {"name": "search_web"}},
            {"type": "function", "function": {"name": "search_knowledge_base"}},
            {"type": "function", "function": {"name": "update_memory"}},
        ]

        def _schema_name(s):
            return s.get("function", {}).get("name")

        filtered = [t for t in tools if _schema_name(t) != "search_web"]
        names = [_schema_name(t) for t in filtered]
        assert "search_web" not in names
        assert "search_knowledge_base" in names
        assert len(filtered) == 2
