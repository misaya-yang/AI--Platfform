"""Provider-boundary regressions for tool history, streaming, and secret safety."""

from __future__ import annotations

import inspect
import json
import logging
import tempfile
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from assistant_service.core.agent.stream_helpers import merge_stream_tool_calls
from assistant_service.core.models import model_registry as model_registry_module
from assistant_service.core.models.model_registry import (
    ChatMessage,
    ModelInfo,
    ModelProvider,
    ModelRegistry,
    ProviderStreamError,
    _raise_for_status_without_query_secrets,
    _request_without_query_secrets,
    _safe_request_error,
)
from assistant_service.core.models.provider_errors import (
    ProviderStreamError as CanonicalProviderStreamError,
)
from assistant_service.core.models.request_safety import (
    _raise_for_status_without_query_secrets as canonical_raise_for_status_without_query_secrets,
)
from assistant_service.core.models.request_safety import (
    _request_without_query_secrets as canonical_request_without_query_secrets,
)
from assistant_service.core.models.request_safety import (
    _safe_request_error as canonical_safe_request_error,
)


def test_provider_stream_error_facade_preserves_identity_and_legacy_module() -> None:
    assert ProviderStreamError is CanonicalProviderStreamError
    assert ProviderStreamError.__module__ == (
        "assistant_service.core.models.model_registry"
    )

    exc = ProviderStreamError("anthropic", "rate_limit_error")

    assert exc.provider == "anthropic"
    assert exc.error_type == "rate_limit_error"
    assert str(exc) == "anthropic stream failed (rate_limit_error)"


def test_provider_stream_error_facade_remains_a_monkeypatch_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SentinelProviderStreamError(RuntimeError):
        pass

    monkeypatch.setattr(
        model_registry_module,
        "ProviderStreamError",
        SentinelProviderStreamError,
    )

    with pytest.raises(SentinelProviderStreamError) as exc_info:
        model_registry_module._parse_sse_event("{", provider="patched")

    assert exc_info.value.args == ("patched", "invalid_sse_json")


def test_request_safety_facade_preserves_identity_and_signatures() -> None:
    cases = (
        (
            _request_without_query_secrets,
            canonical_request_without_query_secrets,
            "(request: 'httpx.Request') -> 'httpx.Request'",
        ),
        (
            _raise_for_status_without_query_secrets,
            canonical_raise_for_status_without_query_secrets,
            "(response: 'Any') -> 'None'",
        ),
        (
            _safe_request_error,
            canonical_safe_request_error,
            "(error: 'httpx.RequestError') -> 'httpx.RequestError'",
        ),
    )

    for facade, canonical, expected_signature in cases:
        assert facade is canonical
        assert str(inspect.signature(facade)) == expected_signature


class _FakeResponse:
    def __init__(
        self,
        *,
        lines: list[str] | None = None,
        status_code: int = 200,
        url: str = "https://provider.test/stream",
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> None:
        self._lines = list(lines or [])
        self._body = body
        self.status_code = status_code
        self.request = httpx.Request("POST", url, headers=request_headers)
        self.headers = httpx.Headers(headers or {})

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "raw provider failure",
                request=self.request,
                response=httpx.Response(
                    self.status_code,
                    request=self.request,
                    content=self._body,
                ),
            )

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return self._body


class _StreamContext:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        enter_error: Exception | None = None,
    ) -> None:
        self._response = response
        self._enter_error = enter_error

    async def __aenter__(self) -> _FakeResponse:
        if self._enter_error is not None:
            raise self._enter_error
        assert self._response is not None
        return self._response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeClient:
    def __init__(self, context: _StreamContext) -> None:
        self._context = context
        self.request_body: dict[str, Any] | None = None

    def stream(self, _method: str, _url: str, *, json: dict[str, Any]) -> _StreamContext:
        self.request_body = json
        return self._context


@pytest.mark.asyncio
async def test_request_safety_facade_remains_a_model_registry_monkeypatch_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    response = _FakeResponse()

    def record_status_check(actual_response: object) -> None:
        events.append(("status", actual_response))

    monkeypatch.setattr(
        model_registry_module,
        "_raise_for_status_without_query_secrets",
        record_status_check,
    )

    async with model_registry_module._safe_provider_stream(
        _FakeClient(_StreamContext(response)),
        "https://provider.test/stream",
        {"stream": True},
    ) as yielded_response:
        events.append(("yield", yielded_response))

    assert events == [("status", response), ("yield", response)]

    request = httpx.Request("POST", "https://provider.test/stream")
    transport_error = httpx.ConnectError("raw transport error", request=request)
    replacement_error = httpx.RequestError("safe transport error", request=request)

    def replace_transport_error(error: httpx.RequestError) -> httpx.RequestError:
        events.append(("sanitize", error))
        return replacement_error

    monkeypatch.setattr(
        model_registry_module,
        "_safe_request_error",
        replace_transport_error,
    )

    with pytest.raises(httpx.RequestError) as exc_info:
        async with model_registry_module._safe_provider_stream(
            _FakeClient(_StreamContext(enter_error=transport_error)),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    assert exc_info.value is replacement_error
    assert events[-1] == ("sanitize", transport_error)


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}"


@pytest.mark.asyncio
async def test_model_database_catalog_failure_redacts_exception_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "private-database-connection-payload"

    class _FailingModelService:
        async def list_models(self, **_kwargs: Any) -> list[dict[str, Any]]:
            raise RuntimeError(f"database response included {sentinel}")

    registry = ModelRegistry(use_default_models=False)
    with caplog.at_level(logging.WARNING, logger=ModelRegistry.__module__):
        loaded = await registry.load_models_from_database(_FailingModelService())

    assert loaded == 0
    assert sentinel not in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_model_database_row_failure_redacts_exception_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "private-model-row-payload"

    class _ExplodingPrice:
        def __float__(self) -> float:
            raise RuntimeError(f"model metadata included {sentinel}")

    class _ModelService:
        async def list_models(self, **_kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "provider_id": "openai",
                    "model_id": "model-safe-id",
                    "display_name": "Safe display name",
                    "access_level": "public",
                    "input_price_per_1k": _ExplodingPrice(),
                }
            ]

    registry = ModelRegistry(use_default_models=False)
    with caplog.at_level(logging.WARNING, logger=ModelRegistry.__module__):
        loaded = await registry.load_models_from_database(_ModelService())

    assert loaded == 0
    assert sentinel not in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_database_catalog_rows_preserve_valid_access_and_skip_dirty_rows() -> None:
    registry = ModelRegistry(use_default_models=False)

    loaded = registry.replace_models_from_database_rows(
        [
            {
                "provider_id": "openai",
                "model_id": "public-model",
                "display_name": "Public model",
                "access_level": "public",
            },
            {
                "provider_id": "openai",
                "model_id": "premium-model",
                "display_name": "Premium model",
                "access_level": "premium",
            },
            {
                "provider_id": "openai",
                "model_id": "admin-model",
                "display_name": "Admin model",
                "access_level": "admin",
            },
            {
                "provider_id": "openai",
                "model_id": "dirty-model",
                "display_name": "Dirty model",
                "access_level": "corrupt",
            },
        ]
    )

    assert loaded == 3
    assert registry.get_model("public-model").access_level.value == "public"
    assert registry.get_model("premium-model").access_level.value == "premium"
    assert registry.get_model("admin-model").access_level.value == "admin"
    assert registry.get_model("dirty-model") is None


@pytest.mark.asyncio
async def test_runtime_memory_provider_failure_redacts_exception_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistant_service.core.agent.agent_loop import AgentLoopPhase
    from assistant_service.core.agent.middlewares import runtime_memory as runtime_memory_module

    sentinel = "private-memory-provider-payload"
    runtime = SimpleNamespace(
        load_memory_context=AsyncMock(
            side_effect=RuntimeError(f"upstream response included {sentinel}")
        ),
        schedule_daily_reflection=AsyncMock(return_value=None),
    )
    ctx = SimpleNamespace(
        tenant_id="tenant_a",
        user_id="user_a",
        message="hello",
        config=SimpleNamespace(
            runtime_mode="compat",
            memory_mode="on",
            memory_profile="basic",
            agent_runtime=None,
        ),
        run_id="run-a",
        session_id="session-a",
    )

    with caplog.at_level(logging.ERROR, logger=runtime_memory_module.__name__):
        events = [
            event
            async for event in runtime_memory_module.RuntimeMemoryMiddleware(
                runtime,
                AgentLoopPhase.MEMORY_LOADING,
            ).before_call(ctx, [])
        ]

    assert events == []
    assert sentinel not in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_google_body_does_not_log_tool_name_or_thought_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool_name = "private_capability_name"
    signature = "private-thought-signature"
    registry = ModelRegistry(use_default_models=False)

    with caplog.at_level(logging.DEBUG):
        registry._build_google_body(
            "gemini-test",
            [
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": "{}"},
                            "thoughtSignature": signature,
                        }
                    ],
                )
            ],
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )

    assert tool_name not in caplog.text
    assert signature not in caplog.text
    assert "name_hash=" in caplog.text


@pytest.mark.parametrize("arguments", ["not-json", "[]", "1"])
def test_google_body_rejects_non_object_tool_arguments(arguments: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    with pytest.raises(ValueError, match="Google tool arguments") as exc_info:
        registry._build_google_body(
            "gemini-test",
            [
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "write_data", "arguments": arguments},
                        }
                    ],
                )
            ],
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_qwen_thinking_honors_explicit_packet_output_ceiling() -> None:
    registry = ModelRegistry()
    explicit = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        temperature=0,
        max_tokens=2048,
        tools=None,
        stream=True,
        thinking_level="high",
    )
    defaulted = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [ChatMessage(role="user", content="hello")],
        temperature=0,
        max_tokens=None,
        tools=None,
        stream=True,
        thinking_level="high",
    )

    assert explicit["max_tokens"] == 2048
    assert explicit["enable_thinking"] is True
    # DashScope rejects (or runs unbounded) hybrid-thinking requests without
    # an output token cap, so when the caller supplies none the provider
    # floor must be restored instead of omitting the field.
    assert defaulted["max_tokens"] == 16384
    assert defaulted["enable_thinking"] is True


def test_qwen_chat_body_marks_stable_system_prefix_for_explicit_cache() -> None:
    registry = ModelRegistry()

    body = registry._build_request_body(
        ModelProvider.DASHSCOPE,
        "qwen3.7-plus",
        [
            ChatMessage(role="system", content="stable agent instructions"),
            ChatMessage(role="user", content="hello"),
        ],
        temperature=0,
        max_tokens=2048,
        tools=[{"type": "function", "function": {"name": "tool_search"}}],
        stream=True,
        thinking_level="medium",
    )

    assert body["messages"][0]["content"] == [
        {
            "type": "text",
            "text": "stable agent instructions",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_non_dashscope_chat_body_does_not_emit_qwen_cache_extension() -> None:
    registry = ModelRegistry(use_default_models=False)

    body = registry._build_openai_body(
        "qwen3.7-plus",
        [ChatMessage(role="system", content="stable agent instructions")],
        temperature=0,
        max_tokens=2048,
        tools=None,
        stream=True,
        provider=ModelProvider.OPENAI,
    )

    assert body["messages"][0]["content"] == "stable agent instructions"


@pytest.mark.parametrize("model_id", ["claude-opus-4-7", "claude-sonnet-4-6"])
def test_current_anthropic_models_advertise_basic_native_search(model_id: str) -> None:
    model = ModelRegistry().get_model(model_id)

    assert model is not None
    assert model.supports_native_search is True
    assert model.native_search_config == {
        "tool_type": "web_search_20250305",
        "max_uses": 5,
    }


def test_anthropic_body_preserves_tool_use_and_parallel_results() -> None:
    registry = ModelRegistry(use_default_models=False)
    body = registry._build_anthropic_body(
        "claude-test",
        [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="look up both"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"query":"alpha"}',
                        },
                    },
                    {
                        "id": "toolu_2",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"id":2}',
                        },
                    },
                ],
            ),
            ChatMessage(
                role="tool",
                content="first result",
                name="search",
                tool_call_id="toolu_1",
            ),
            ChatMessage(
                role="tool",
                content="second result",
                name="lookup",
                tool_call_id="toolu_2",
            ),
        ],
        temperature=0,
        max_tokens=100,
        tools=None,
        stream=True,
    )

    assert [message["role"] for message in body["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    tool_uses = body["messages"][1]["content"]
    assert [(block["id"], block["name"], block["input"]) for block in tool_uses] == [
        ("toolu_1", "search", {"query": "alpha"}),
        ("toolu_2", "lookup", {"id": 2}),
    ]
    tool_results = body["messages"][2]["content"]
    assert [block["tool_use_id"] for block in tool_results] == ["toolu_1", "toolu_2"]


@pytest.mark.parametrize("builder_name", ["_build_anthropic_body", "_build_openai_body"])
def test_provider_body_rejects_unpaired_tool_call_before_network(builder_name: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    builder = getattr(registry, builder_name)

    with pytest.raises(ValueError, match="unpaired tool exchange"):
        builder(
            "provider-test",
            [
                ChatMessage(role="user", content="run the tool"),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "toolu_cancelled",
                            "type": "function",
                            "function": {
                                "name": "slow_tool",
                                "arguments": "{}",
                            },
                        }
                    ],
                ),
            ],
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )


def _unpaired_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="run the tool"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "toolu_cancelled",
                    "type": "function",
                    "function": {"name": "slow_tool", "arguments": "{}"},
                }
            ],
        ),
    ]


def _duplicate_call_messages() -> list[ChatMessage]:
    call = {
        "id": "toolu_dup",
        "type": "function",
        "function": {"name": "slow_tool", "arguments": "{}"},
    }
    return [
        ChatMessage(role="user", content="run the tool"),
        ChatMessage(role="assistant", content="", tool_calls=[call, call]),
    ]


def _complete_pair_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content="run the tool"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "toolu_ok",
                    "type": "function",
                    "function": {"name": "slow_tool", "arguments": "{}"},
                }
            ],
        ),
        ChatMessage(
            role="tool",
            content="cancelled",
            name="slow_tool",
            tool_call_id="toolu_ok",
        ),
    ]


class _RecordingProviderClient:
    def __init__(self) -> None:
        self.posts = 0
        self.streams = 0

    async def post(self, *_args: Any, **_kwargs: Any) -> Any:
        self.posts += 1
        raise AssertionError("provider HTTP POST must not run")

    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        self.streams += 1
        raise AssertionError("provider HTTP stream must not run")


def _registry_with_providers() -> ModelRegistry:
    registry = ModelRegistry(use_default_models=False)
    registry.replace_models_from_database_rows(
        [
            {
                "provider_id": "openai",
                "model_id": "gpt-test",
                "display_name": "GPT test",
                "access_level": "public",
            },
            {
                "provider_id": "anthropic",
                "model_id": "claude-test",
                "display_name": "Claude test",
                "access_level": "public",
            },
        ]
    )
    return registry


@pytest.mark.parametrize("builder_name", ["_build_anthropic_body", "_build_openai_body"])
def test_provider_body_rejects_duplicate_tool_call_before_network(builder_name: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    builder = getattr(registry, builder_name)

    with pytest.raises(ValueError, match="duplicate tool call"):
        builder(
            "provider-test",
            _duplicate_call_messages(),
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )


@pytest.mark.parametrize(
    ("model_id", "messages", "match"),
    [
        ("gpt-test", _unpaired_messages(), "unpaired tool exchange"),
        ("claude-test", _unpaired_messages(), "unpaired tool exchange"),
        ("gpt-test", _duplicate_call_messages(), "duplicate tool call"),
        ("claude-test", _duplicate_call_messages(), "duplicate tool call"),
    ],
)
@pytest.mark.asyncio
async def test_chat_stream_rejects_invalid_tool_transcript_without_http(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    messages: list[ChatMessage],
    match: str,
) -> None:
    registry = _registry_with_providers()
    client = _RecordingProviderClient()
    monkeypatch.setattr(registry, "_get_client", AsyncMock(return_value=client))

    with pytest.raises(ValueError, match=match):
        async for _delta in registry.chat_stream(model_id, messages, temperature=0):
            pass

    assert client.posts == 0
    assert client.streams == 0


@pytest.mark.parametrize(
    ("messages", "match"),
    [
        (_unpaired_messages(), "unpaired tool exchange"),
        (_duplicate_call_messages(), "duplicate tool call"),
        (
            [
                ChatMessage(role="user", content="run the tool"),
                ChatMessage(
                    role="tool",
                    content="private result",
                    name="slow_tool",
                    tool_call_id="private-orphan-call",
                ),
            ],
            "orphan tool result",
        ),
    ],
)
@pytest.mark.asyncio
async def test_responses_v1_stream_rejects_invalid_tool_transcript_without_http(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[ChatMessage],
    match: str,
) -> None:
    registry = _registry_with_providers()
    registry.configure_provider(
        ModelProvider.OPENAI,
        api_key="test-only-provider-key",
        wire_protocol="responses_v1",
    )
    client = _RecordingProviderClient()
    monkeypatch.setattr(registry, "_get_client", AsyncMock(return_value=client))

    with pytest.raises(ValueError, match=match) as exc_info:
        async for _delta in registry.chat_stream("gpt-test", messages, temperature=0):
            pass

    assert "private-" not in str(exc_info.value)
    assert client.posts == 0
    assert client.streams == 0


@pytest.mark.parametrize("builder_name", ["_build_anthropic_body", "_build_openai_body"])
def test_provider_body_accepts_complete_tool_pair(builder_name: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    builder = getattr(registry, builder_name)
    body = builder(
        "provider-test",
        _complete_pair_messages(),
        temperature=0,
        max_tokens=100,
        tools=None,
        stream=True,
    )
    assert body["messages"]


@pytest.mark.parametrize("builder_name", ["_build_anthropic_body", "_build_openai_body"])
def test_provider_body_rejects_orphan_tool_result_before_network(builder_name: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    builder = getattr(registry, builder_name)

    with pytest.raises(ValueError, match="orphan tool result"):
        builder(
            "provider-test",
            [
                ChatMessage(role="user", content="run the tool"),
                ChatMessage(
                    role="tool",
                    content="cancelled",
                    name="slow_tool",
                    tool_call_id="toolu_missing",
                ),
            ],
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )


@pytest.mark.parametrize("arguments", ["not-json", "[]", "1"])
def test_anthropic_body_rejects_non_object_tool_arguments(arguments: str) -> None:
    registry = ModelRegistry(use_default_models=False)
    with pytest.raises(ValueError, match="Anthropic tool arguments") as exc_info:
        registry._build_anthropic_body(
            "claude-test",
            [
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "search", "arguments": arguments},
                        }
                    ],
                )
            ],
            temperature=0,
            max_tokens=100,
            tools=None,
            stream=True,
        )
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.asyncio
async def test_anthropic_stream_reconstructs_parallel_tools_and_cache_usage() -> None:
    lines = [
        _sse(
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 20,
                        "cache_read_input_tokens": 8,
                        "cache_creation_input_tokens": 4,
                    }
                },
            }
        ),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "search",
                    "input": {},
                },
            }
        ),
        _sse(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "lookup",
                    "input": {},
                },
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"id":'},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"alpha"}'},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": "2}"},
            }
        ),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse({"type": "content_block_stop", "index": 1}),
        _sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 12},
            }
        ),
        _sse({"type": "message_stop"}),
    ]
    response = _FakeResponse(lines=lines)
    client = _FakeClient(_StreamContext(response))
    registry = ModelRegistry(use_default_models=False)

    deltas = [
        delta
        async for delta in registry._stream_anthropic(client, "/v1/messages", {"stream": True})
    ]

    accumulator: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    counter = 0
    for delta in deltas:
        if delta.tool_calls:
            counter = merge_stream_tool_calls(delta.tool_calls, accumulator, order, counter)
    calls = [accumulator[key] for key in order]
    assert [(call["id"], call["function"]["name"]) for call in calls] == [
        ("toolu_1", "search"),
        ("toolu_2", "lookup"),
    ]
    assert [json.loads(call["function"]["arguments"]) for call in calls] == [
        {"q": "alpha"},
        {"id": 2},
    ]
    assert deltas[0].usage == {
        "input_tokens": 20,
        "cached_input_tokens": 8,
        "cache_read_input_tokens": 8,
        "cache_creation_input_tokens": 4,
    }
    assert deltas[-1].finish_reason == "tool_use"
    assert deltas[-1].usage == {"output_tokens": 12}


@pytest.mark.asyncio
async def test_anthropic_stream_error_is_typed_and_prompt_safe() -> None:
    response = _FakeResponse(
        lines=[
            _sse({"type": "message_start", "message": {"usage": {}}}),
            _sse(
                {
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": "secret-prompt-sentinel",
                    },
                }
            ),
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.provider == "anthropic"
    assert exc_info.value.error_type == "overloaded_error"
    assert "secret-prompt-sentinel" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_stream_normalizes_untrusted_error_type() -> None:
    sentinel = "secret-error-type-sentinel"
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "type": "error",
                    "error": {"type": sentinel, "message": "provider failure"},
                }
            )
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "provider_error"
    assert sentinel not in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_stream_rejects_truncated_tool_input() -> None:
    response = _FakeResponse(
        lines=[
            _sse({"type": "message_start", "message": {"usage": {}}}),
            _sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "write_data",
                        "input": {},
                    },
                }
            ),
            _sse(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"value":',
                    },
                }
            ),
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_tool_use"


@pytest.mark.asyncio
async def test_anthropic_stream_requires_terminal_message_stop() -> None:
    response = _FakeResponse(
        lines=[
            _sse({"type": "message_start", "message": {"usage": {}}}),
            _sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "write_data",
                        "input": {},
                    },
                }
            ),
            _sse(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"value":1}',
                    },
                }
            ),
            _sse({"type": "content_block_stop", "index": 0}),
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_message"


def test_http_status_error_removes_query_keys_and_response_body() -> None:
    secret = "AQ.vertex-secret-789"
    request = httpx.Request(
        "POST",
        f"https://provider.test/generate?key={secret}&alt=sse",
    )
    response = httpx.Response(
        429,
        request=request,
        content=b"secret-provider-body",
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _raise_for_status_without_query_secrets(response)

    error = exc_info.value
    assert secret not in str(error)
    assert secret not in str(error.request.url)
    assert "key=" not in str(error.request.url)
    assert "alt=sse" in str(error.request.url)
    assert error.response.content == b""


@pytest.mark.asyncio
async def test_google_stream_does_not_log_or_dump_request_body(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    sentinel = "raw-prompt-sentinel-should-never-leak"
    capability_sentinel = "secret-capability-sentinel"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": capability_sentinel,
                                            "args": {"value": "private-argument"},
                                        },
                                        "thoughtSignature": "private-signature",
                                    }
                                ]
                            },
                        }
                    ]
                }
            ),
            _sse(
                {
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": capability_sentinel,
                                            "args": {"value": "private-argument"},
                                        }
                                    }
                                ]
                            },
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 1},
                }
            ),
        ],
        url="https://provider.test/stream?key=secret&alt=sse",
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": sentinel}]}],
        "tools": [
            {"functionDeclarations": [{"name": "private_capability", "description": sentinel}]}
        ],
    }
    registry = ModelRegistry(use_default_models=False)

    with caplog.at_level(logging.DEBUG):
        deltas = [
            delta
            async for delta in registry._stream_google(
                _FakeClient(_StreamContext(response)),
                "https://provider.test/stream?key=secret&alt=sse",
                body,
            )
        ]

    assert deltas[-1].usage == {"input_tokens": 1}
    assert sentinel not in caplog.text
    assert "private_capability" not in caplog.text
    assert capability_sentinel not in caplog.text
    assert "private-argument" not in caplog.text
    assert "private-signature" not in caplog.text
    assert not (tmp_path / "gemini_last_body.json").exists()


@pytest.mark.asyncio
async def test_google_stream_error_keeps_key_and_body_out_of_exception_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "AQ.vertex-secret-987"
    body_secret = b"provider-body-secret"
    response = _FakeResponse(
        status_code=429,
        url=f"https://provider.test/stream?key={secret}&alt=sse",
        body=body_secret,
        headers={"x-request-id": "request-safe-id"},
    )
    registry = ModelRegistry(use_default_models=False)

    with caplog.at_level(logging.ERROR), pytest.raises(httpx.HTTPStatusError) as exc_info:
        async for _ in registry._stream_google(
            _FakeClient(_StreamContext(response)),
            f"https://provider.test/stream?key={secret}&alt=sse",
            {"contents": []},
        ):
            pass

    assert secret not in str(exc_info.value)
    assert secret not in str(exc_info.value.request.url)
    assert body_secret.decode() not in caplog.text
    assert "body_bytes=20" in caplog.text
    assert "request-safe-id" in caplog.text


@pytest.mark.asyncio
async def test_google_transport_error_removes_query_key() -> None:
    secret = "AQ.vertex-secret-654"
    request = httpx.Request(
        "POST",
        f"https://provider.test/stream?key={secret}&alt=sse",
    )
    transport_error = httpx.ConnectError("connect failed", request=request)
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(httpx.RequestError) as exc_info:
        async for _ in registry._stream_google(
            _FakeClient(_StreamContext(enter_error=transport_error)),
            str(request.url),
            {"contents": []},
        ):
            pass

    assert isinstance(exc_info.value, httpx.ConnectError)
    assert exc_info.value.__context__ is None
    assert secret not in str(exc_info.value.request.url)
    assert "key=" not in str(exc_info.value.request.url)
    assert "alt=sse" in str(exc_info.value.request.url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_method", "provider"),
    [
        ("_stream_openai", "openai-compatible"),
        ("_stream_anthropic", "anthropic"),
        ("_stream_google", "google"),
    ],
)
async def test_provider_streams_fail_closed_on_malformed_sse_json(
    stream_method: str,
    provider: str,
) -> None:
    sentinel = "private-malformed-sse-sentinel"
    response = _FakeResponse(lines=[f'data: {{"partial":"{sentinel}'])
    registry = ModelRegistry(use_default_models=False)
    stream = getattr(registry, stream_method)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in stream(
            _FakeClient(_StreamContext(response)),
            "https://provider.test/stream",
            {"contents": []},
        ):
            pass

    assert exc_info.value.provider == provider
    assert exc_info.value.error_type == "invalid_sse_json"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert sentinel not in str(exc_info.value)


@pytest.mark.asyncio
async def test_google_unknown_block_reason_is_not_logged_or_reflected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "untrusted-block-reason\nforged-log-entry"
    response = _FakeResponse(lines=[_sse({"promptFeedback": {"blockReason": sentinel}})])
    registry = ModelRegistry(use_default_models=False)

    with caplog.at_level(logging.WARNING):
        deltas = [
            delta
            async for delta in registry._stream_google(
                _FakeClient(_StreamContext(response)),
                "https://provider.test/stream",
                {"contents": []},
            )
        ]

    assert sentinel not in caplog.text
    assert "UNKNOWN" in caplog.text
    assert sentinel not in deltas[0].content
    assert "UNKNOWN" in deltas[0].content


@pytest.mark.asyncio
async def test_google_api_key_header_stays_out_of_httpx_url_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "AQ.httpx-log-secret"
    registry = ModelRegistry(use_default_models=False)
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key=secret)
    endpoint = registry._vertex_endpoint("gemini-test", stream=False)
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["key"] = request.headers.get("x-goog-api-key", "")
        return httpx.Response(200, request=request, json={"candidates": []})

    transport = httpx.MockTransport(handler)
    headers = registry._build_headers(ModelProvider.GOOGLE_VERTEX, secret)
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with httpx.AsyncClient(transport=transport, headers=headers) as client:
            await client.post(endpoint, json={"contents": []})

    assert observed["key"] == secret
    assert secret not in observed["url"]
    assert "key=" not in observed["url"]
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_call_requires_terminal_stream_evidence() -> None:
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_data",
                                            "arguments": '{"value":1}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(response)),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_message"


@pytest.mark.asyncio
async def test_google_tool_call_requires_terminal_stream_evidence() -> None:
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "write_data",
                                            "args": {"value": 1},
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_google(
            _FakeClient(_StreamContext(response)),
            "https://provider.test/stream",
            {"contents": []},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_message"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason",
    ["length", "content_filter"],
)
async def test_openai_rejects_truncated_tool_call_terminal_reason(
    finish_reason: str,
) -> None:
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_data",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            _sse({"choices": [{"delta": {}, "finish_reason": finish_reason}]}),
            "data: [DONE]",
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(response)),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_tool_call"


@pytest.mark.asyncio
async def test_openai_tool_terminal_allows_trailing_usage_only_chunk() -> None:
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_data",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            _sse(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }
            ),
            "data: [DONE]",
        ]
    )
    registry = ModelRegistry(use_default_models=False)
    deltas = [
        delta
        async for delta in registry._stream_openai(
            _FakeClient(_StreamContext(response)),
            "/v1/chat/completions",
            {"stream": True},
        )
    ]

    assert deltas[-1].usage == {"input_tokens": 7, "output_tokens": 3}


@pytest.mark.asyncio
async def test_google_rejects_truncated_tool_call_terminal_reason() -> None:
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "candidates": [
                        {
                            "finishReason": "MAX_TOKENS",
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "write_data",
                                            "args": {},
                                        }
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_google(
            _FakeClient(_StreamContext(response)),
            "https://provider.test/stream",
            {"contents": []},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_tool_call"


@pytest.mark.asyncio
async def test_anthropic_rejects_truncated_tool_call_terminal_reason() -> None:
    response = _FakeResponse(
        lines=[
            _sse({"type": "message_start", "message": {"usage": {}}}),
            _sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "write_data",
                        "input": {},
                    },
                }
            ),
            _sse({"type": "content_block_stop", "index": 0}),
            _sse(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "max_tokens"},
                    "usage": {},
                }
            ),
            _sse({"type": "message_stop"}),
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "incomplete_tool_call"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_method", ["_stream_openai", "_stream_google"])
async def test_tool_event_after_terminal_is_protocol_error(stream_method: str) -> None:
    if stream_method == "_stream_openai":
        lines = [
            _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            _sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_data",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
        ]
    else:
        lines = [
            _sse({"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}),
            _sse(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"functionCall": {"name": "write_data", "args": {}}}]
                            }
                        }
                    ]
                }
            ),
        ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in getattr(registry, stream_method)(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "event_after_terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_before_start", [False, True])
async def test_anthropic_rejects_tool_events_outside_streaming_phase(
    tool_before_start: bool,
) -> None:
    message_start = _sse({"type": "message_start", "message": {"usage": {}}})
    terminal = _sse(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {},
        }
    )
    tool_start = _sse(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "write_data",
                "input": {},
            },
        }
    )
    if tool_before_start:
        lines = [tool_start, message_start, terminal, _sse({"type": "message_stop"})]
        expected = "invalid_event_order"
    else:
        lines = [message_start, terminal, tool_start, _sse({"type": "message_stop"})]
        expected = "event_after_terminal"
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_method", "auth_header"),
    [
        ("_stream_openai", "authorization"),
        ("_stream_anthropic", "x-api-key"),
    ],
)
async def test_non_google_stream_http_errors_drop_credentials_and_body(
    stream_method: str,
    auth_header: str,
) -> None:
    secret = "private-provider-credential"
    response = _FakeResponse(
        status_code=401,
        body=b"private-provider-error-body",
        request_headers={auth_header: secret},
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        async for _ in getattr(registry, stream_method)(
            _FakeClient(_StreamContext(response)),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    error = exc_info.value
    assert secret not in str(error)
    assert auth_header not in error.request.headers
    assert error.response.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_method", "auth_header"),
    [
        ("_stream_openai", "authorization"),
        ("_stream_anthropic", "x-api-key"),
    ],
)
async def test_non_google_stream_transport_errors_drop_request_secrets(
    stream_method: str,
    auth_header: str,
) -> None:
    secret = "private-provider-credential"
    request = httpx.Request(
        "POST",
        "https://provider.test/stream",
        headers={auth_header: secret},
        content=b"private-request-body",
    )
    transport_error = httpx.ConnectError("private-connect-detail", request=request)
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(httpx.RequestError) as exc_info:
        async for _ in getattr(registry, stream_method)(
            _FakeClient(_StreamContext(enter_error=transport_error)),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert "private-connect-detail" not in str(error)
    assert auth_header not in error.request.headers
    assert error.request.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_method", "provider"),
    [
        ("_stream_openai", "openai-compatible"),
        ("_stream_anthropic", "anthropic"),
        ("_stream_google", "google"),
    ],
)
async def test_provider_streams_normalize_non_object_events(
    stream_method: str,
    provider: str,
) -> None:
    sentinel = "private-non-object-event"
    response = _FakeResponse(lines=[f'data: ["{sentinel}"]'])
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in getattr(registry, stream_method)(
            _FakeClient(_StreamContext(response)),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    assert exc_info.value.provider == provider
    assert exc_info.value.error_type == "invalid_event"
    assert sentinel not in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_invalid_index_is_typed_and_prompt_safe() -> None:
    sentinel = "private-index-sentinel"
    response = _FakeResponse(
        lines=[
            _sse({"type": "message_start", "message": {"usage": {}}}),
            _sse(
                {
                    "type": "content_block_start",
                    "index": sentinel,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(response)),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_event"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert sentinel not in str(exc_info.value)


@pytest.mark.asyncio
async def test_anthropic_native_server_tool_blocks_are_not_local_tool_calls() -> None:
    lines = [
        _sse({"type": "message_start", "message": {"usage": {}}}),
        _sse({"type": "ping"}),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {},
                },
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"query":"weather"}',
                },
            }
        ),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_1",
                    "content": [],
                },
            }
        ),
        _sse({"type": "content_block_stop", "index": 1}),
        _sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            }
        ),
        _sse({"type": "ping"}),
        _sse({"type": "future_event", "payload": "ignored"}),
        _sse({"type": "message_stop"}),
    ]
    registry = ModelRegistry(use_default_models=False)

    deltas = [
        delta
        async for delta in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        )
    ]

    assert all(not delta.tool_calls for delta in deltas)
    assert deltas[-1].finish_reason == "end_turn"
    assert deltas[-1].usage == {"output_tokens": 4}
    assert deltas[-1].provider_content_blocks == [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_1",
            "content": [],
        },
    ]


@pytest.mark.asyncio
async def test_anthropic_content_blocks_cannot_start_after_message_delta_phase() -> None:
    lines = [
        _sse({"type": "message_start", "message": {"usage": {}}}),
        _sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": None},
                "usage": {"output_tokens": 1},
            }
        ),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "write_data",
                    "input": {},
                },
            }
        ),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "event_after_terminal"


@pytest.mark.asyncio
async def test_anthropic_invalid_tool_json_does_not_retain_arguments_chain() -> None:
    sentinel = "private-tool-argument-sentinel"
    lines = [
        _sse({"type": "message_start", "message": {"usage": {}}}),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "write_data",
                    "input": {},
                },
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": f'{{"private":"{sentinel}"',
                },
            }
        ),
        _sse({"type": "content_block_stop", "index": 0}),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    error = exc_info.value
    assert error.error_type == "invalid_tool_input_json"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sentinel not in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_type", ["future_argument_delta", "text_delta"])
async def test_anthropic_non_argument_client_tool_delta_fails_closed(
    delta_type: str,
) -> None:
    lines = [
        _sse({"type": "message_start", "message": {"usage": {}}}),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "write_data",
                    "input": {},
                },
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": delta_type,
                    "private": "must-not-be-ignored",
                },
            }
        ),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_tool_input"


@pytest.mark.asyncio
async def test_openai_qwen_tool_continuations_strip_empty_identity_fields() -> None:
    """Qwen repeats empty identity fields while streaming argument fragments."""

    first_arguments = '{"todos":['
    final_arguments = '{"content":"review","status":"pending"}]}'
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_qwen_1",
                                    "type": "function",
                                    "function": {
                                        "name": "todo_write",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "type": "function",
                                    "function": {
                                        "name": "",
                                        "arguments": first_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "type": "function",
                                    "function": {"arguments": final_arguments},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        _sse(
            {
                "choices": [],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        ),
        "data: [DONE]",
    ]
    registry = ModelRegistry(use_default_models=False)

    deltas = [
        delta
        async for delta in registry._stream_openai(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/chat/completions",
            {"stream": True},
        )
    ]

    tool_deltas = [delta.tool_calls for delta in deltas if delta.tool_calls]
    assert "id" not in tool_deltas[1][0]
    assert "name" not in tool_deltas[1][0]["function"]
    assert "id" not in tool_deltas[2][0]

    accumulator: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    counter = 0
    for chunks in tool_deltas:
        counter = merge_stream_tool_calls(chunks, accumulator, order, counter)

    assert [accumulator[key] for key in order] == [
        {
            "id": "call_qwen_1",
            "type": "function",
            "function": {
                "name": "todo_write",
                "arguments": first_arguments + final_arguments,
            },
        }
    ]
    assert any(delta.finish_reason == "tool_calls" for delta in deltas)
    assert deltas[-1].usage == {"input_tokens": 11, "output_tokens": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "orphan_call",
    [
        {
            "index": 7,
            "id": "",
            "type": "function",
            "function": {"name": "todo_write", "arguments": "{}"},
        },
        {
            "index": 7,
            "type": "function",
            "function": {"arguments": "{}"},
        },
    ],
)
async def test_openai_rejects_orphan_tool_continuation(
    orphan_call: dict[str, Any],
) -> None:
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [orphan_call]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_event"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rebound_field",
    [
        {"id": "call_rebound", "function": {"arguments": "{}"}},
        {"function": {"name": "different_tool", "arguments": "{}"}},
    ],
)
async def test_openai_rejects_tool_continuation_identity_rebinding(
    rebound_field: dict[str, Any],
) -> None:
    initial_call = {
        "index": 0,
        "id": "call_qwen_1",
        "type": "function",
        "function": {"name": "todo_write", "arguments": ""},
    }
    continuation = {"index": 0, "type": "function", **rebound_field}
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [initial_call]},
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [continuation]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_event"


@pytest.mark.asyncio
async def test_openai_rejects_tool_call_id_reuse_across_indexes() -> None:
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_shared",
                                    "type": "function",
                                    "function": {"name": "todo_write", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_shared",
                                    "type": "function",
                                    "function": {"name": "todo_write", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_event"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_call",
    [
        "malformed-private-args-delta",
        {"index": 0, "function": {"future_private_field": "sentinel"}},
    ],
)
async def test_openai_malformed_partial_tool_delta_fails_closed(
    malformed_call: Any,
) -> None:
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "write_data",
                                        "arguments": "",
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [malformed_call]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
    ]
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "invalid_event"


@pytest.mark.asyncio
async def test_openai_stream_error_event_is_typed_and_prompt_safe() -> None:
    sentinel = "private-openai-provider-message"
    response = _FakeResponse(
        lines=[
            _sse(
                {
                    "error": {
                        "type": "server_error",
                        "message": sentinel,
                    }
                }
            )
        ]
    )
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in registry._stream_openai(
            _FakeClient(_StreamContext(response)),
            "/v1/chat/completions",
            {"stream": True},
        ):
            pass

    assert exc_info.value.error_type == "server_error"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert sentinel not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_method", "lines", "provider"),
    [
        (
            "_stream_openai",
            [
                _sse(
                    {
                        "choices": [
                            {
                                "delta": {"content": "partial"},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            ],
            "openai-compatible",
        ),
        (
            "_stream_google",
            [_sse({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})],
            "google",
        ),
    ],
)
async def test_provider_text_stream_requires_terminal_evidence(
    stream_method: str,
    lines: list[str],
    provider: str,
) -> None:
    registry = ModelRegistry(use_default_models=False)

    with pytest.raises(ProviderStreamError) as exc_info:
        async for _ in getattr(registry, stream_method)(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "https://provider.test/stream",
            {"stream": True},
        ):
            pass

    assert exc_info.value.provider == provider
    assert exc_info.value.error_type == "incomplete_message"


@pytest.mark.asyncio
async def test_anthropic_pause_turn_reconstructs_citations_for_verbatim_replay() -> None:
    citation = {
        "type": "web_search_result_location",
        "cited_text": "weather result",
        "url": "https://example.test/weather",
        "title": "Weather",
        "encrypted_index": "opaque-index",
    }
    lines = [
        _sse({"type": "message_start", "message": {"usage": {}}}),
        _sse(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": "", "citations": []},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Result"},
            }
        ),
        _sse(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "citations_delta", "citation": citation},
            }
        ),
        _sse({"type": "content_block_stop", "index": 0}),
        _sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "pause_turn"},
                "usage": {"output_tokens": 5},
            }
        ),
        _sse({"type": "message_stop"}),
    ]
    registry = ModelRegistry(use_default_models=False)

    deltas = [
        delta
        async for delta in registry._stream_anthropic(
            _FakeClient(_StreamContext(_FakeResponse(lines=lines))),
            "/v1/messages",
            {"stream": True},
        )
    ]
    blocks = deltas[-1].provider_content_blocks
    assert blocks == [
        {
            "type": "text",
            "text": "Result",
            "citations": [citation],
        }
    ]

    replay_body = registry._build_anthropic_body(
        "claude-test",
        [
            ChatMessage(role="user", content="search"),
            ChatMessage(
                role="assistant",
                content="Result",
                provider_content_blocks=blocks,
            ),
        ],
        temperature=0,
        max_tokens=100,
        tools=None,
        stream=True,
        native_search_config={"tool_type": "web_search_20250305", "max_uses": 5},
    )

    assert replay_body["messages"][-1]["content"] == blocks


def test_anthropic_mixed_server_and_client_blocks_round_trip_with_tool_result() -> None:
    registry = ModelRegistry(use_default_models=False)
    blocks = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "write_data",
            "input": {"value": 1},
        },
    ]
    body = registry._build_anthropic_body(
        "claude-test",
        [
            ChatMessage(role="user", content="search and write"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "write_data",
                            "arguments": '{"value":1}',
                        },
                    }
                ],
                provider_content_blocks=blocks,
            ),
            ChatMessage(
                role="tool",
                content="written",
                name="write_data",
                tool_call_id="toolu_1",
            ),
        ],
        temperature=0,
        max_tokens=100,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "write_data",
                    "description": "Write data",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        stream=True,
        native_search_config={"tool_type": "web_search_20250305", "max_uses": 5},
    )

    assert body["messages"][1] == {"role": "assistant", "content": blocks}
    assert body["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert [tool["name"] for tool in body["tools"]] == ["write_data", "web_search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [ModelProvider.OPENAI, ModelProvider.ANTHROPIC])
@pytest.mark.parametrize("failure_mode", ["http", "transport"])
async def test_non_google_chat_errors_drop_request_credentials_and_payload(
    provider: ModelProvider,
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "private-chat-credential"

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_mode == "transport":
            raise httpx.ConnectError("private-transport-detail", request=request)
        return httpx.Response(
            401,
            request=request,
            content=b"private-response-body",
        )

    client = httpx.AsyncClient(
        base_url="https://provider.test",
        headers={"authorization" if provider == ModelProvider.OPENAI else "x-api-key": secret},
        transport=httpx.MockTransport(handler),
    )
    registry = ModelRegistry(use_default_models=False)
    model_id = f"{provider.value}-safe-error-test"
    registry.add_custom_model(ModelInfo(id=model_id, name=model_id, provider=provider))

    async def fake_get_client(
        _provider: ModelProvider,
        *,
        model_id: str | None = None,
    ) -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(registry, "_get_client", fake_get_client)
    expected_error = httpx.HTTPStatusError if failure_mode == "http" else httpx.RequestError
    try:
        with pytest.raises(expected_error) as exc_info:
            await registry.chat(model_id, [ChatMessage(role="user", content="hello")])
    finally:
        await client.aclose()

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert not ({"authorization", "x-api-key"} & set(error.request.headers))
    assert error.request.content == b""
    if isinstance(error, httpx.HTTPStatusError):
        assert error.response.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider",
    [ModelProvider.OPENAI, ModelProvider.ANTHROPIC, ModelProvider.GOOGLE],
)
async def test_nonstream_malformed_json_is_typed_without_response_chain(
    provider: ModelProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "private-nonstream-json-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            content=f'{{"private":"{sentinel}"'.encode(),
        )

    client = httpx.AsyncClient(
        base_url="https://provider.test",
        transport=httpx.MockTransport(handler),
    )
    registry = ModelRegistry(use_default_models=False)
    model_id = f"{provider.value}-invalid-json-test"
    registry.add_custom_model(ModelInfo(id=model_id, name=model_id, provider=provider))

    async def fake_get_client(
        _provider: ModelProvider,
        *,
        model_id: str | None = None,
    ) -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(registry, "_get_client", fake_get_client)
    with pytest.raises(ProviderStreamError) as exc_info:
        await registry.chat(model_id, [ChatMessage(role="user", content="hello")])

    if not client.is_closed:
        await client.aclose()
    error = exc_info.value
    assert error.error_type == "invalid_response_json"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert sentinel not in str(error)
