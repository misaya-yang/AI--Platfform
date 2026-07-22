"""
Model Registry - Unified interface for multiple LLM providers.

Supports (default catalog as of 2026-04):
- OpenAI (gpt-4o, o1)
- Anthropic (claude-opus-4-5, claude-sonnet-4-5)
- DeepSeek (deepseek-chat, deepseek-reasoner)
- DashScope/Qwen (qwen3.7-plus, qwen3.6-plus, qwen-max)
- Google / Google Vertex (gemini-3-pro-preview, gemini-3-flash-preview)
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from ai_gateway_core.enums import ModelAccessLevel, ModelProvider
from ai_gateway_core.logging import get_logger
from ai_gateway_core.models import ChatMessage
from ai_gateway_core.models import normalize_chat_message as _normalize_message

from ..quality.cache_optimizer import normalize_provider_cache_usage

# Re-export so existing ``from ...model_registry import ModelProvider`` sites
# keep working. Phase 5d moved the enum definitions to ``ai_gateway_core``
# so gateway routes (health, assistant) can import the enum without pulling
# in the full registry. Delete re-export once AS-internal call sites migrate.
__all__ = ["ModelAccessLevel", "ModelProvider", "ModelInfo", "ModelRegistry"]

logger = get_logger(__name__)


@dataclass
class ModelInfo:
    """Model metadata."""

    id: str
    name: str
    provider: ModelProvider
    context_window: int = 128000
    max_output_tokens: int = 4096
    supports_vision: bool = False
    supports_tools: bool = True
    input_price_per_1k: float = 0.0  # USD per 1K tokens
    output_price_per_1k: float = 0.0
    access_level: ModelAccessLevel = ModelAccessLevel.PUBLIC  # Permission level required

    # --- Native web-search capability (DERIVED — not persisted, not UI-editable) ---
    # Populated from ``NATIVE_SEARCH_CAPABLE`` in ``__post_init__`` based on the
    # (provider, model_id) pair. Intentionally NOT exposed in the Model
    # Management UI because the capability requires provider-specific
    # request-body wiring in ``_build_*_body`` (e.g. Anthropic's
    # ``web_search_20250305`` tool, Gemini's ``google_search`` tool,
    # DashScope's ``enable_search`` flag). Flipping a DB boolean without a
    # matching code path would produce 400 errors — so the map is the
    # single source of truth and DB-loaded ``ModelInfo`` instances pick
    # it up transparently on construction.
    supports_native_search: bool = False
    native_search_config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.supports_native_search and self.native_search_config is None:
            cfg = NATIVE_SEARCH_CAPABLE.get((self.provider, self.id))
            if cfg is not None:
                self.supports_native_search = True
                self.native_search_config = cfg


# Hardcoded capability map: (provider, model_id) -> provider-specific config
# that will be merged into the request body when native search is activated.
# Keeping this here (not per-ModelInfo field) so DB-backed and default models
# both benefit and we have one place to update when providers change APIs.
NATIVE_SEARCH_CAPABLE: dict[tuple[ModelProvider, str], dict[str, Any]] = {
    # DashScope / Qwen — `enable_search: true` in extra_body (OpenAI-compat).
    # Ref: https://help.aliyun.com/zh/model-studio/qwen-web-search
    # Note: qwen-turbo / qwen-plus retired from catalog (2026-04).
    (ModelProvider.DASHSCOPE, "qwen-max"): {"enable_search": True},
    (ModelProvider.DASHSCOPE, "qwen3.7-plus"): {"enable_search": True},
    (ModelProvider.DASHSCOPE, "qwen3.6-plus"): {"enable_search": True},
    # Google Gemini — `google_search` tool (2.0+).
    # Ref: https://ai.google.dev/gemini-api/docs/grounding
    # Note: Gemini 2.5 family retired from catalog (2026-04) in favor of 3.x.
    (ModelProvider.GOOGLE, "gemini-3.1-pro-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE, "gemini-3.1-flash-lite-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE, "gemini-3-pro-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE, "gemini-3-flash-preview"): {"tool_type": "google_search"},
    # Vertex entries mirror the Google ones — same wire format, same tool_type,
    # different host. Kept explicit (rather than branching on provider at
    # lookup time) so the capability map stays uniform across providers.
    (ModelProvider.GOOGLE_VERTEX, "gemini-3.1-pro-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE_VERTEX, "gemini-3.1-flash-lite-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE_VERTEX, "gemini-3-pro-preview"): {"tool_type": "google_search"},
    (ModelProvider.GOOGLE_VERTEX, "gemini-3-flash-preview"): {"tool_type": "google_search"},
    # Anthropic — server tool `web_search_20250305`.
    # Ref: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/web-search-tool
    # Note: Claude 3.5 and Sonnet-4 (2025-05) retired in favor of 4.5 family.
    (ModelProvider.ANTHROPIC, "claude-opus-4-7"): {
        "tool_type": "web_search_20250305",
        "max_uses": 5,
    },
    (ModelProvider.ANTHROPIC, "claude-opus-4-5"): {
        "tool_type": "web_search_20250305",
        "max_uses": 5,
    },
    (ModelProvider.ANTHROPIC, "claude-sonnet-4-6"): {
        "tool_type": "web_search_20250305",
        "max_uses": 5,
    },
    (ModelProvider.ANTHROPIC, "claude-sonnet-4-5"): {
        "tool_type": "web_search_20250305",
        "max_uses": 5,
    },
    # OpenAI — deferred: Responses API `web_search_preview` is not supported by
    # our /chat/completions path. DeepSeek has no native search. Tavily fallback.
}


# Simple heuristic: does the user's message look like it wants fresh web info?
# Used to decide whether to enable native search for this turn. Kept tiny and
# dependency-free; ``web_fetch`` is the URL-fetch fallback for everything else.
_SEARCH_HINT_KEYWORDS = (
    # English
    "search",
    "latest",
    "news",
    "today",
    "current",
    "recent",
    "who is",
    "what is happening",
    "stock price",
    "weather",
    # Chinese
    "搜索",
    "查一下",
    "查询",
    "最新",
    "今天",
    "新闻",
    "现在",
    "最近",
    # Arabic
    "ابحث",
    "أخبار",
    "اليوم",
    "الآن",
)


def should_use_native_search(user_message: str) -> bool:
    """Return True if the message looks like it needs fresh web info.

    Intentionally permissive on search intent but conservative on non-search
    prompts (e.g. "write a poem" returns False).
    """
    if not user_message:
        return False
    lowered = user_message.lower()
    return any(kw in lowered for kw in _SEARCH_HINT_KEYWORDS)


def _sanitize_usage(raw_usage: dict[str, Any]) -> dict[str, int]:
    """
    Sanitize and normalize usage dict.

    - Only include integer values and known cache-token fields
    - Normalize OpenAI keys (prompt_tokens -> input_tokens, completion_tokens -> output_tokens)

    Some providers (e.g., DashScope) return nested dicts like:
    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    """
    return normalize_provider_cache_usage(raw_usage)


def _request_without_query_secrets(request: httpx.Request) -> httpx.Request:
    """Return a metadata-only request safe to attach to provider errors."""
    url = request.url
    for parameter in ("key", "api_key"):
        url = url.copy_remove_param(parameter)
    return httpx.Request(request.method, url)


def _raise_for_status_without_query_secrets(response: Any) -> None:
    """Raise an HTTP error without retaining provider keys or response bodies."""
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status_code < 300:
        return

    request = getattr(response, "request", None)
    if not isinstance(request, httpx.Request):
        request = httpx.Request("POST", "https://provider.invalid/")
    safe_request = _request_without_query_secrets(request)
    safe_response = httpx.Response(
        status_code or 500,
        request=safe_request,
    )
    raise httpx.HTTPStatusError(
        f"Provider returned HTTP {status_code or 500}",
        request=safe_request,
        response=safe_response,
    )


def _safe_request_error(error: httpx.RequestError) -> httpx.RequestError:
    """Replace a transport error with a query-secret-free equivalent."""
    request = getattr(error, "request", None)
    if not isinstance(request, httpx.Request):
        request = httpx.Request("POST", "https://provider.invalid/")
    safe_request = _request_without_query_secrets(request)
    try:
        return type(error)("Provider request failed", request=safe_request)
    except TypeError:
        return httpx.RequestError(
            "Provider request failed",
            request=safe_request,
        )


@contextlib.asynccontextmanager
async def _safe_provider_stream(
    client: httpx.AsyncClient,
    endpoint: str,
    body: dict[str, Any],
) -> AsyncIterator[Any]:
    """Open a provider stream without retaining credentials or response bodies."""

    safe_transport_error: httpx.RequestError | None = None
    try:
        async with client.stream("POST", endpoint, json=body) as response:
            _raise_for_status_without_query_secrets(response)
            yield response
    except httpx.HTTPStatusError:
        raise
    except httpx.RequestError as exc:
        safe_transport_error = _safe_request_error(exc)
    if safe_transport_error is not None:
        raise safe_transport_error


def _parse_sse_event(data: str, *, provider: str) -> dict[str, Any]:
    """Parse one SSE payload without retaining malformed provider data in an exception."""

    invalid_json = False
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        invalid_json = True
        event = None
    if invalid_json:
        raise ProviderStreamError(provider, "invalid_sse_json")
    if not isinstance(event, dict):
        raise ProviderStreamError(provider, "invalid_event")
    return event


def _validate_openai_tool_call_deltas(raw_calls: Any) -> list[dict[str, Any]] | None:
    """Validate every partial tool call before it can reach the executor."""

    if raw_calls is None:
        return None
    if not isinstance(raw_calls, list):
        raise ProviderStreamError("openai-compatible", "invalid_event")
    validated: list[dict[str, Any]] = []
    for call in raw_calls:
        if not isinstance(call, dict) or not call:
            raise ProviderStreamError("openai-compatible", "invalid_event")
        if "index" in call:
            index = call["index"]
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ProviderStreamError("openai-compatible", "invalid_event")
        if "id" in call and (not isinstance(call["id"], str) or not call["id"]):
            raise ProviderStreamError("openai-compatible", "invalid_event")
        if "type" in call and call["type"] != "function":
            raise ProviderStreamError("openai-compatible", "invalid_event")
        function = call.get("function")
        if function is not None:
            if (
                not isinstance(function, dict)
                or not function
                or not ({"name", "arguments"} & set(function))
            ):
                raise ProviderStreamError("openai-compatible", "invalid_event")
            if "name" in function and (
                not isinstance(function["name"], str) or not function["name"]
            ):
                raise ProviderStreamError("openai-compatible", "invalid_event")
            if "arguments" in function and not isinstance(function["arguments"], str):
                raise ProviderStreamError("openai-compatible", "invalid_event")
        elif "id" not in call:
            raise ProviderStreamError("openai-compatible", "invalid_event")
        validated.append(call)
    return validated


# --- Streaming smoother for Vertex-style chunked upstreams ---
# Vertex Express Mode flushes SSE frames ~1/sec with ~100 chars each — the
# frontend then renders those as 3-6 big jumps and the user perceives it as
# "not streaming." We split each Vertex frame into smaller sub-deltas with
# a small inter-chunk delay so the UI sees a token-like cadence without
# materially changing total stream time.
#
# Also applies to the OpenAI-compat path (DashScope, DeepSeek, OpenAI itself)
# as of 2026-04-24 — DashScope Intl's SSE coalesces multiple tokens per frame
# on slower networks, and without splitting the UI rendered in 2 large bursts
# instead of a smooth stream. The same smoother works for both providers.
#
# Operator override: ``GEMINI_SMOOTHER_DISABLED=1`` turns this off for all
# providers (the provider then yields each upstream frame verbatim). The env
# name is kept historical — it now gates Google AND OpenAI-compat, renaming
# would require re-plumbing across prod deploys. Useful for debugging
# "is the smoother introducing artificial latency?" and nothing else — in
# production the smoother is always on.
_SMOOTHER_DISABLED = os.environ.get("GEMINI_SMOOTHER_DISABLED", "").lower() in {"1", "true", "yes"}
_SMOOTHER_CHARS_PER_CHUNK = 4
_SMOOTHER_DELAY_SECONDS = 0.020
_SMOOTHER_MIN_TEXT_LEN = 12  # chunks smaller than this don't benefit from splitting


async def _smooth_text_delta(text: str) -> AsyncIterator[str]:
    """Split a large Vertex text frame into smaller sub-deltas.

    For a 200-char Vertex frame this yields ~50 sub-chunks at ~20ms intervals,
    which the frontend renders as ~25 char/s typewriter flow — noticeably
    streamy, total extra latency ~1s (well below the original 1-second
    gap between Vertex frames so no net regression on total time).
    """
    if _SMOOTHER_DISABLED or len(text) <= _SMOOTHER_MIN_TEXT_LEN:
        yield text
        return
    import asyncio as _asyncio

    chunk_size = _SMOOTHER_CHARS_PER_CHUNK
    for i in range(0, len(text), chunk_size):
        yield text[i : i + chunk_size]
        # Skip sleep on the final slice — no one sees it
        if i + chunk_size < len(text):
            await _asyncio.sleep(_SMOOTHER_DELAY_SECONDS)


@dataclass
class StreamDelta:
    """A single streaming delta from the model."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    thought_signature: str | None = None  # Gemini 3 thought signature
    thinking_content: str | None = None  # Qwen reasoning_content / Gemini thought parts
    # Complete provider-native assistant blocks emitted once the message is
    # closed. Used only when the provider requires verbatim continuation.
    provider_content_blocks: list[dict[str, Any]] | None = None


class ProviderStreamError(RuntimeError):
    """Prompt-safe provider stream failure surfaced to the runtime boundary."""

    def __init__(self, provider: str, error_type: str) -> None:
        self.provider = provider
        self.error_type = error_type
        super().__init__(f"{provider} stream failed ({error_type})")


_SAFE_ANTHROPIC_ERROR_TYPES = frozenset(
    {
        "api_error",
        "authentication_error",
        "billing_error",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
    }
)

_SAFE_OPENAI_FINISH_REASONS = frozenset(
    {"stop", "length", "tool_calls", "content_filter", "function_call"}
)
_SAFE_OPENAI_ERROR_TYPES = frozenset(
    {
        "authentication_error",
        "invalid_request_error",
        "not_found_error",
        "permission_error",
        "rate_limit_error",
        "server_error",
    }
)
_SAFE_ANTHROPIC_STOP_REASONS = frozenset(
    {
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "model_context_window_exceeded",
    }
)
_SAFE_GOOGLE_FINISH_REASONS = frozenset(
    {
        "STOP",
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "OTHER",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "MALFORMED_FUNCTION_CALL",
        "IMAGE_SAFETY",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
    }
)

_SAFE_GOOGLE_BLOCK_REASONS = frozenset(
    {
        "BLOCKLIST",
        "IMAGE_SAFETY",
        "OTHER",
        "PROHIBITED_CONTENT",
        "SAFETY",
    }
)


@dataclass
class ModelConfig:
    """Configuration for a model provider."""

    api_key: str
    base_url: str | None = None
    timeout: float = 300.0
    max_retries: int = 2
    #: Backend flavor for Google provider only — ``"ai_studio"`` (default,
    #: ``generativelanguage.googleapis.com/v1beta``) or ``"vertex"``
    #: (``aiplatform.googleapis.com/v1/publishers/google``). Ignored for
    #: other providers. Express Mode Vertex keys (``AQ.xxx``) work as
    #: drop-in replacements — no OAuth / project / location required,
    #: which is why we only support Express Mode for now.
    backend: str = "ai_studio"


# Env-driven routing for the Google provider:
#   ``GOOGLE_API_BACKEND``     — global default, ``ai_studio`` (default) | ``vertex``
#   ``GOOGLE_VERTEX_MODELS``   — comma-separated model IDs that should always
#                                go to Vertex regardless of global default.
#                                Handy for A/B testing one model at a time.
#   ``VERTEX_API_KEY``         — Express-Mode key (``AQ.xxx``). Falls back to
#                                ``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` if unset.
# These are read in ``main.py`` at provider-configuration time and applied
# via ``configure_provider(backend=...)``. Per-model overrides are resolved
# by the endpoint and header helpers so they don't require reconfiguration.


# Default model catalog
#
# All prices are in **USD per 1K tokens** (list price per 1M ÷ 1000),
# verified against each provider's official pricing page on 2026-04-22:
#   - Google:    https://ai.google.dev/gemini-api/docs/pricing
#   - Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
#   - OpenAI:    https://developers.openai.com/api/docs/pricing
#   - DeepSeek:  https://api-docs.deepseek.com/quick_start/pricing/
#   - DashScope: https://www.alibabacloud.com/help/en/model-studio/models
#
# For providers with tiered context-length pricing (Gemini 3 / 3.1 Pro,
# Gemini 2.5 Pro) we record the ≤200K tier — long-context requests are
# rare in our workload. Operators needing long-context cost tracking can
# override prices per model via the Model Management UI.
DEFAULT_MODELS: dict[ModelProvider, list[ModelInfo]] = {
    ModelProvider.OPENAI: [
        # GPT-5 family — current flagship on OpenAI's pricing page
        # (verified 2026-04-22). gpt-5.4 is the recommended production
        # model; Mini / Nano are cheaper tiers for routing & bulk work.
        # Legacy gpt-4o / o1 kept as fallbacks for old history keys.
        ModelInfo(
            id="gpt-5.4",
            name="GPT-5.4",
            provider=ModelProvider.OPENAI,
            context_window=400000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0025,  # $2.50 per 1M
            output_price_per_1k=0.015,  # $15.00 per 1M
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="gpt-5.4-mini",
            name="GPT-5.4 Mini",
            provider=ModelProvider.OPENAI,
            context_window=400000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.00075,  # $0.75 per 1M
            output_price_per_1k=0.0045,  # $4.50 per 1M
            access_level=ModelAccessLevel.PREMIUM,
        ),
        ModelInfo(
            id="gpt-5.4-nano",
            name="GPT-5.4 Nano",
            provider=ModelProvider.OPENAI,
            context_window=400000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0002,  # $0.20 per 1M
            output_price_per_1k=0.00125,  # $1.25 per 1M
            access_level=ModelAccessLevel.PUBLIC,
        ),
        ModelInfo(
            id="gpt-4o",
            name="GPT-4o (legacy)",
            provider=ModelProvider.OPENAI,
            context_window=128000,
            max_output_tokens=16384,
            supports_vision=True,
            input_price_per_1k=0.0025,  # $2.50 per 1M — unchanged
            output_price_per_1k=0.01,  # $10.00 per 1M — unchanged
        ),
        ModelInfo(
            id="o1",
            name="O1 (legacy reasoning)",
            provider=ModelProvider.OPENAI,
            context_window=200000,
            max_output_tokens=100000,
            supports_vision=True,
            input_price_per_1k=0.015,
            output_price_per_1k=0.06,
            access_level=ModelAccessLevel.ADMIN,
        ),
    ],
    ModelProvider.ANTHROPIC: [
        # Opus 4.5/4.6/4.7 share base pricing per Anthropic's page
        # (verified 2026-04-22): $5 in / $25 out per 1M. Sonnet 4.5/4.6
        # also share pricing ($3 / $15 per 1M). Haiku 4.5 is $1 / $5.
        # Keep separate entries per model id so usage records identify
        # exactly which version served the turn.
        ModelInfo(
            id="claude-opus-4-7",
            name="Claude Opus 4.7",
            provider=ModelProvider.ANTHROPIC,
            context_window=1000000,
            max_output_tokens=64000,
            supports_vision=True,
            input_price_per_1k=0.005,  # $5 per 1M
            output_price_per_1k=0.025,  # $25 per 1M
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="claude-opus-4-5",
            name="Claude Opus 4.5",
            provider=ModelProvider.ANTHROPIC,
            context_window=1000000,
            max_output_tokens=64000,
            supports_vision=True,
            # Previous catalog had 0.015/0.075 (Opus 4 / 4.1 pricing).
            # Opus 4.5 cut prices 67% vs 4.1 per Anthropic's blog.
            input_price_per_1k=0.005,  # $5 per 1M — CORRECTED
            output_price_per_1k=0.025,  # $25 per 1M — CORRECTED
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="claude-sonnet-4-6",
            name="Claude Sonnet 4.6",
            provider=ModelProvider.ANTHROPIC,
            context_window=1000000,
            max_output_tokens=64000,
            supports_vision=True,
            input_price_per_1k=0.003,  # $3 per 1M
            output_price_per_1k=0.015,  # $15 per 1M
            access_level=ModelAccessLevel.PREMIUM,
        ),
        ModelInfo(
            id="claude-sonnet-4-5",
            name="Claude Sonnet 4.5",
            provider=ModelProvider.ANTHROPIC,
            context_window=1000000,
            max_output_tokens=64000,
            supports_vision=True,
            input_price_per_1k=0.003,  # $3 per 1M — unchanged
            output_price_per_1k=0.015,  # $15 per 1M — unchanged
        ),
        ModelInfo(
            id="claude-haiku-4-5",
            name="Claude Haiku 4.5",
            provider=ModelProvider.ANTHROPIC,
            context_window=200000,
            max_output_tokens=64000,
            supports_vision=True,
            input_price_per_1k=0.001,  # $1 per 1M
            output_price_per_1k=0.005,  # $5 per 1M
        ),
    ],
    ModelProvider.DEEPSEEK: [
        # DeepSeek V3.2 unified chat + reasoner on the same price
        # (verified at api-docs.deepseek.com 2026-04-22):
        #   cache-miss input: $0.28 per 1M → 0.00028 per 1K
        #   output:           $0.42 per 1M → 0.00042 per 1K
        # Cache hits cost $0.028/1M; UsageRecorder counts billable tokens
        # on the wire — the cache discount is applied at bill time.
        # Context bumped to 128K (was 64K) per V3.2 release notes.
        ModelInfo(
            id="deepseek-chat",
            name="DeepSeek Chat (V3.2)",
            provider=ModelProvider.DEEPSEEK,
            context_window=128000,  # bumped 64K → 128K per V3.2
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.00028,  # $0.28 per 1M — unchanged
            output_price_per_1k=0.00042,  # $0.42 per 1M — unchanged
        ),
        ModelInfo(
            id="deepseek-reasoner",
            name="DeepSeek Reasoner (V3.2)",
            provider=ModelProvider.DEEPSEEK,
            context_window=128000,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.00028,  # Unified with chat on V3.2
            output_price_per_1k=0.00042,
        ),
    ],
    ModelProvider.DASHSCOPE: [
        # Qwen pricing — international (Singapore) DashScope endpoint,
        # verified 2026-04-22. Mainland CN endpoint is cheaper but our
        # gateway targets international.
        ModelInfo(
            id="qwen3.7-plus",
            name="Qwen 3.7 Plus",
            provider=ModelProvider.DASHSCOPE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=False,
            input_price_per_1k=0.0005,
            output_price_per_1k=0.003,
        ),
        ModelInfo(
            id="qwen3.6-plus",
            name="Qwen 3.6 Plus",
            provider=ModelProvider.DASHSCOPE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=False,
            # Prior catalog had 0/0 which broke cost tracking. Official
            # DashScope global pricing (≤256K request): $0.50 / $3.00 per 1M.
            input_price_per_1k=0.0005,  # $0.50 per 1M — FIXED (was 0)
            output_price_per_1k=0.003,  # $3.00 per 1M — FIXED (was 0)
        ),
        ModelInfo(
            id="qwen-max",
            name="Qwen Max",
            provider=ModelProvider.DASHSCOPE,
            context_window=32768,
            max_output_tokens=8192,
            supports_vision=False,
            # Official global rate: $1.60 input / $6.40 output per 1M.
            # Previous catalog had $1.20/$6.00 (pre-2026 price).
            input_price_per_1k=0.0016,  # $1.60 per 1M — CORRECTED
            output_price_per_1k=0.0064,  # $6.40 per 1M — CORRECTED
        ),
    ],
    ModelProvider.GOOGLE: [
        # Gemini 3.1 family — released April 2026 (blog.google 2026-04-17)
        # and now live on ai.google.dev/gemini-api/docs/pricing. The
        # earlier comment "3.1 not released as of 2026-04" was based on
        # info that has since become stale — 3.1 Pro Preview and 3.1
        # Flash Lite Preview are callable today. Pro uses tiered pricing;
        # we record the ≤200K tier.
        ModelInfo(
            id="gemini-3.1-pro-preview",
            name="Gemini 3.1 Pro",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.002,  # $2 per 1M (≤200K tier)
            output_price_per_1k=0.012,  # $12 per 1M (≤200K tier)
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="gemini-3.1-flash-lite-preview",
            name="Gemini 3.1 Flash Lite",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.00025,  # $0.25 per 1M
            output_price_per_1k=0.0015,  # $1.50 per 1M
            access_level=ModelAccessLevel.PUBLIC,
        ),
        # Gemini 3 preview series — retained for history-key resolution.
        ModelInfo(
            id="gemini-3-pro-preview",
            name="Gemini 3 Pro",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.002,  # $2 per 1M (≤200K) — unchanged
            output_price_per_1k=0.012,  # $12 per 1M (≤200K) — unchanged
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="gemini-3-flash-preview",
            name="Gemini 3 Flash",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0005,  # $0.50 per 1M — unchanged
            output_price_per_1k=0.003,  # $3 per 1M — unchanged
            access_level=ModelAccessLevel.ADMIN,
        ),
    ],
    # Vertex AI default catalog — mirrors the Google (AI Studio) Gemini set.
    # Same model IDs on purpose: Gemini-on-Vertex and Gemini-on-AI-Studio
    # target the same underlying models, so keeping IDs identical means the
    # ModelRegistry can resolve a ``gemini-2.5-flash`` request against
    # whichever provider the caller picks.
    ModelProvider.GOOGLE_VERTEX: [
        # Mirrors ModelProvider.GOOGLE — same IDs on purpose so the registry
        # can resolve the same model_id against whichever provider the caller
        # picks. Kept in sync with the Google block above (2.5 family retired,
        # 3.1 family added). Display names suffixed " (Vertex)" so both
        # providers can coexist visually in the Model Management UI.
        ModelInfo(
            id="gemini-3.1-pro-preview",
            name="Gemini 3.1 Pro (Vertex)",
            provider=ModelProvider.GOOGLE_VERTEX,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.002,  # $2 per 1M (≤200K tier)
            output_price_per_1k=0.012,  # $12 per 1M (≤200K tier)
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="gemini-3.1-flash-lite-preview",
            name="Gemini 3.1 Flash Lite (Vertex)",
            provider=ModelProvider.GOOGLE_VERTEX,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.00025,  # $0.25 per 1M
            output_price_per_1k=0.0015,  # $1.50 per 1M
            access_level=ModelAccessLevel.PUBLIC,
        ),
        ModelInfo(
            id="gemini-3-pro-preview",
            name="Gemini 3 Pro (Vertex)",
            provider=ModelProvider.GOOGLE_VERTEX,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.002,
            output_price_per_1k=0.012,
            access_level=ModelAccessLevel.ADMIN,
        ),
        ModelInfo(
            id="gemini-3-flash-preview",
            name="Gemini 3 Flash (Vertex)",
            provider=ModelProvider.GOOGLE_VERTEX,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0005,
            output_price_per_1k=0.003,
            access_level=ModelAccessLevel.ADMIN,
        ),
    ],
}


class ModelRegistry:
    """
    Registry for managing multiple LLM providers.

    Provides a unified interface for:
    - Model discovery and metadata
    - Chat completions (streaming and non-streaming)
    - Provider-specific API handling
    """

    # Default base URLs for each provider
    DEFAULT_BASE_URLS = {
        ModelProvider.OPENAI: "https://api.openai.com",
        ModelProvider.ANTHROPIC: "https://api.anthropic.com",
        ModelProvider.DEEPSEEK: "https://api.deepseek.com",
        ModelProvider.DASHSCOPE: "https://dashscope.aliyuncs.com/compatible-mode",
        ModelProvider.GOOGLE: "https://generativelanguage.googleapis.com",
        ModelProvider.GOOGLE_VERTEX: "https://aiplatform.googleapis.com",
    }

    #: Base URL used when a Google provider is configured with ``backend="vertex"``.
    #: Separate from ``DEFAULT_BASE_URLS`` because the same ``ModelProvider.GOOGLE``
    #: enum value routes to two completely different hosts depending on backend.
    VERTEX_BASE_URL = "https://aiplatform.googleapis.com"

    def __init__(self, use_default_models: bool = True):
        self._configs: dict[ModelProvider, ModelConfig] = {}
        self._models: dict[str, ModelInfo] = {}
        self._clients: dict[ModelProvider, httpx.AsyncClient] = {}
        self._db_models_loaded: bool = False

        # Initialize default model catalog (fallback)
        if use_default_models:
            for _provider, models in DEFAULT_MODELS.items():
                for model in models:
                    self._models[model.id] = model

    async def load_models_from_database(self, model_service, tenant_id: str = "default") -> int:
        """
        Load models from database, replacing default models.

        Args:
            model_service: ModelService instance
            tenant_id: Tenant ID to load models for

        Returns:
            Number of models loaded
        """
        try:
            db_models = await model_service.list_models(
                tenant_id=tenant_id,
                include_disabled=False,
            )

            if not db_models:
                logger.info("No models in database, keeping default catalog")
                return 0

            # Clear existing models and load from database
            self._models.clear()
            loaded_count = 0
            for row in db_models:
                try:
                    # Map provider_id to ModelProvider enum
                    provider_id = row.get("provider_id", "")
                    try:
                        provider = ModelProvider(provider_id)
                    except ValueError:
                        # Unknown provider, skip
                        logger.warning(
                            f"Unknown provider {provider_id} for model {row.get('model_id')}"
                        )
                        continue

                    # Map access_level string to enum
                    access_str = row.get("access_level", "public")
                    try:
                        access_level = ModelAccessLevel(access_str)
                    except ValueError:
                        access_level = ModelAccessLevel.PUBLIC

                    model = ModelInfo(
                        id=row["model_id"],
                        name=row["display_name"],
                        provider=provider,
                        context_window=row.get("context_window", 128000),
                        max_output_tokens=row.get("max_output_tokens", 4096),
                        supports_vision=row.get("supports_vision", False),
                        supports_tools=row.get("supports_tools", True),
                        input_price_per_1k=float(row.get("input_price_per_1k", 0)),
                        output_price_per_1k=float(row.get("output_price_per_1k", 0)),
                        access_level=access_level,
                    )
                    self._models[model.id] = model
                    loaded_count += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to load model row (exception_type=%s)",
                        type(exc).__name__,
                    )

            self._db_models_loaded = True
            logger.info(f"Loaded {loaded_count} models from database")
            return loaded_count

        except Exception as exc:
            logger.warning(
                "Failed to load models from database (exception_type=%s)",
                type(exc).__name__,
            )
            return 0

    def clear_models(self) -> None:
        """Clear all models from registry."""
        self._models.clear()
        self._db_models_loaded = False

    def configure_provider(
        self,
        provider: ModelProvider,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 120.0,
        backend: str = "ai_studio",
    ) -> None:
        """Configure a provider with API credentials.

        ``backend`` is meaningful only for ``ModelProvider.GOOGLE`` — either
        ``"ai_studio"`` (default, ``generativelanguage.googleapis.com``) or
        ``"vertex"`` (``aiplatform.googleapis.com``). When ``"vertex"`` and
        no explicit ``base_url`` is passed, the Vertex base URL is selected
        automatically so callers don't have to know the host format.
        """
        resolved_base = base_url
        if resolved_base is None:
            if provider == ModelProvider.GOOGLE and backend == "vertex":
                resolved_base = self.VERTEX_BASE_URL
            else:
                resolved_base = self.DEFAULT_BASE_URLS.get(provider)

        self._configs[provider] = ModelConfig(
            api_key=api_key,
            base_url=resolved_base,
            timeout=timeout,
            backend=backend if provider == ModelProvider.GOOGLE else "ai_studio",
        )
        # Reset client if exists
        if provider in self._clients:
            # Don't close here to avoid async issues
            del self._clients[provider]

    def is_provider_configured(self, provider: ModelProvider) -> bool:
        """Check if a provider is configured."""
        return provider in self._configs and bool(self._configs[provider].api_key)

    def _google_backend_for_model(self, model_id: str) -> str:
        """Resolve the effective Google backend for one model.

        Precedence (first match wins):
          1. ``GOOGLE_VERTEX_MODELS`` env — comma-separated list; if
             ``model_id`` is in it, force ``vertex``.
          2. ``ModelConfig.backend`` on the Google provider config — set at
             startup from ``GOOGLE_API_BACKEND`` in ``main.py``.
          3. Default: ``ai_studio``.
        """
        vertex_models_env = os.environ.get("GOOGLE_VERTEX_MODELS", "").strip()
        if vertex_models_env and model_id in {
            m.strip() for m in vertex_models_env.split(",") if m.strip()
        }:
            return "vertex"
        cfg = self._configs.get(ModelProvider.GOOGLE)
        return cfg.backend if cfg else "ai_studio"

    def _google_endpoint(self, model_id: str, *, stream: bool) -> str:
        """Build the **absolute** endpoint URL for a Google call.

        Returns a full URL (not a path) so that per-model backend overrides
        via ``GOOGLE_VERTEX_MODELS`` work even when the provider's global
        client has a different base_url — httpx treats absolute URLs as
        overrides, so we can route individual models without recreating
        the HTTP client.

        Supports two endpoints:
          * AI Studio: ``https://generativelanguage.googleapis.com/v1beta/models/{m}:{action}``
          * Vertex Express Mode: ``https://aiplatform.googleapis.com/v1/publishers/google/models/{m}:{action}``

        Authentication is supplied through the ``x-goog-api-key`` request
        header. API keys must never be embedded in the URL because HTTP
        client access logs routinely include the complete request target.
        """
        backend = self._google_backend_for_model(model_id)
        action = "streamGenerateContent" if stream else "generateContent"
        if backend == "vertex":
            base = self.VERTEX_BASE_URL
            path = f"/v1/publishers/google/models/{model_id}:{action}"
        else:
            base = self.DEFAULT_BASE_URLS[ModelProvider.GOOGLE]
            path = f"/v1beta/models/{model_id}:{action}"
        if stream:
            path += "?alt=sse"
        return f"{base}{path}"

    def _vertex_endpoint(self, model_id: str, *, stream: bool) -> str:
        """Build the absolute Vertex Express-Mode endpoint URL.

        Used when a model's ``provider == ModelProvider.GOOGLE_VERTEX``.
        Mirrors the Vertex branch of ``_google_endpoint`` but sources the
        key from the GOOGLE_VERTEX provider config (not VERTEX_API_KEY env)
        because the whole point of making Vertex its own provider is that
        operators configure it through the Service Management UI — the DB
        row is the source of truth.

        ``model_id`` may carry a ``-vertex`` suffix that's a DB-only
        disambiguator used to let the same underlying Gemini model coexist
        under both the ``google`` (AI Studio) provider and ``google-vertex``
        without colliding on the old 2-column PK. The suffix is stripped
        here — Google's Vertex API only knows the bare id (e.g.
        ``gemini-3-flash-preview``). Safe no-op when a row already uses
        a canonical id.
        """
        config = self._configs.get(ModelProvider.GOOGLE_VERTEX)
        base = (config.base_url if config else None) or self.VERTEX_BASE_URL
        upstream_id = model_id[: -len("-vertex")] if model_id.endswith("-vertex") else model_id
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1/publishers/google/models/{upstream_id}:{action}"
        if stream:
            path += "?alt=sse"
        return f"{base}{path}"

    def _google_api_key_for_model(
        self,
        provider: ModelProvider,
        model_id: str | None,
    ) -> str:
        """Return the request key without ever placing it in a URL."""

        config = self._configs.get(provider)
        configured_key = config.api_key if config else ""
        if (
            provider == ModelProvider.GOOGLE
            and model_id
            and self._google_backend_for_model(model_id) == "vertex"
        ):
            return os.environ.get("VERTEX_API_KEY", "").strip() or configured_key
        return configured_key

    def get_available_models(self) -> list[ModelInfo]:
        """Get all models from configured providers."""
        available = []
        for model in self._models.values():
            if self.is_provider_configured(model.provider):
                available.append(model)
        return available

    def get_model(self, model_id: str) -> ModelInfo | None:
        """Get model info by ID."""
        return self._models.get(model_id)

    def add_custom_model(self, model: ModelInfo) -> None:
        """Add a custom model to the registry."""
        self._models[model.id] = model

    async def _get_client(
        self,
        provider: ModelProvider,
        *,
        model_id: str | None = None,
    ) -> httpx.AsyncClient:
        """Get or create HTTP client for provider.

        Google providers (both AI Studio and Vertex) use a **fresh client
        per call**. Our earlier mitigation — caching the client with
        ``max_keepalive_connections=0`` — did NOT eliminate the 30-47s
        tail latency observed on the second streaming request of a
        session. Even with keepalive off, httpx's ``AsyncClient``
        maintains internal HTTP/1 connection state per host; an SSE
        stream that finishes normally appears to leave something in a
        half-closed state that the Google servers don't reply to
        promptly.

        The fix is to make the client fully ephemeral for Google: every
        call builds, uses, and closes its own ``AsyncClient`` (see the
        ``finally: await client.aclose()`` in ``chat_stream`` /
        ``chat``). This costs ~150ms per request for TLS, but guarantees
        no shared state between calls. Non-Google providers keep their
        cached, keepalive-enabled client — they don't exhibit the bug.

        Caller contract:
          * Google / Vertex: treat the returned client as single-use.
            ``async with`` it or explicitly ``await client.aclose()``.
          * Everyone else: do NOT close it; the registry owns the lifetime.
        """
        config = self._configs.get(provider)
        if not config:
            raise ValueError(f"Provider {provider} not configured")

        api_key = (
            self._google_api_key_for_model(provider, model_id)
            if provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)
            else config.api_key
        )
        headers = self._build_headers(provider, api_key)
        is_google = provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)

        if is_google:
            # Ephemeral — caller closes.
            return httpx.AsyncClient(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout),
                limits=httpx.Limits(max_keepalive_connections=0, max_connections=2),
            )

        if provider not in self._clients:
            self._clients[provider] = httpx.AsyncClient(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
            )
        return self._clients[provider]

    def _build_headers(self, provider: ModelProvider, api_key: str) -> dict[str, str]:
        """Build headers for API requests."""
        if provider == ModelProvider.ANTHROPIC:
            return {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        elif provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            return {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }
        else:
            # OpenAI-compatible (OpenAI, DeepSeek, DashScope)
            return {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

    def _build_request_body(
        self,
        provider: ModelProvider,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build request body for the provider's API."""
        if provider == ModelProvider.ANTHROPIC:
            return self._build_anthropic_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                native_search_config=native_search_config,
            )
        elif provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            return self._build_google_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                thinking_level,
                tool_config,
                native_search_config=native_search_config,
            )
        else:
            return self._build_openai_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                thinking_level=thinking_level,
                native_search_config=native_search_config,
            )

    def _build_openai_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        thinking_level: str | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build OpenAI-compatible request body."""
        from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

        formatted_messages = []
        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            content = msg.content
            # Anthropic-only cache marker — strip for OpenAI-compat providers.
            if msg.role == "system" and isinstance(content, str) and CACHE_SPLIT_MARKER in content:
                content = content.replace(CACHE_SPLIT_MARKER, "").replace("\n\n\n\n", "\n\n")
            m: dict[str, Any] = {"role": msg.role, "content": content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = [{"type": "text", "text": msg.content}]
                for img in msg.images:
                    if img.startswith("http") or img.startswith("data:"):
                        # URL or data URL - use as-is
                        content_parts.append({"type": "image_url", "image_url": {"url": img}})
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img}"},
                            }
                        )
                m["content"] = content_parts
            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        # DashScope extensions are TOP-LEVEL fields on the compat endpoint —
        # NOT nested under `extra_body`. The `extra_body` dict is a client-
        # side concept in the OpenAI Python SDK (which unpacks it into the
        # request body); we POST raw JSON, so wrapping silently drops the
        # flag. Verified live against qwen3.6-plus on 2026-04-21:
        #   body.extra_body.enable_search = True → flag IGNORED, model
        #     refuses ("I can't fetch real-time data").
        #   body.enable_search = True → flag RESPECTED, model returns
        #     results with real team names and scores.
        # Same applies to enable_thinking.
        if thinking_level and "qwen3" in model_id.lower():
            body["enable_thinking"] = True
            # An explicit caller ceiling is a hard Context Packet boundary.
            # Only choose the provider-friendly default when the caller did
            # not reserve an output budget.
            if "max_tokens" not in body:
                body["max_tokens"] = 16384
        if native_search_config and native_search_config.get("enable_search"):
            # DashScope CN vs Intl differ in where ``enable_search`` belongs:
            #   CN (dashscope.aliyuncs.com):   body.enable_search = True (top-level)
            #     Verified 2026-04-21 — extra_body form is IGNORED on CN.
            #   Intl (dashscope-intl.aliyuncs.com):  body.extra_body.enable_search = True
            #     Verified 2026-04-23 — top-level form returns HTTP 500.
            # Also documented at https://www.alibabacloud.com/help/en/model-studio/web-search —
            # the Intl doc explicitly uses ``extra_body={"enable_search": True,
            # "search_options": {"search_strategy": "agent"}}``.
            provider = ModelProvider.DASHSCOPE
            cfg = self._configs.get(provider) if provider in self._configs else None
            cfg_base = (cfg.base_url if cfg else "") or ""
            if "-intl" in cfg_base:
                body["extra_body"] = {
                    **(body.get("extra_body") or {}),
                    "enable_search": True,
                    "search_options": {"search_strategy": "agent"},
                }
            else:
                body["enable_search"] = True
        return body

    def _build_anthropic_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Anthropic-specific request body."""
        system_prompt = None
        formatted_messages = []

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_prompt = msg.content
                continue

            if msg.role == "tool":
                if not msg.tool_call_id:
                    raise ValueError("Anthropic tool results require a non-empty tool_call_id")
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": str(msg.tool_call_id),
                    "content": str(msg.content or ""),
                }
                if (
                    formatted_messages
                    and formatted_messages[-1].get("role") == "user"
                    and isinstance(formatted_messages[-1].get("content"), list)
                    and all(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in formatted_messages[-1]["content"]
                    )
                ):
                    formatted_messages[-1]["content"].append(tool_result)
                else:
                    formatted_messages.append({"role": "user", "content": [tool_result]})
                continue

            if msg.role == "assistant" and msg.provider_content_blocks is not None:
                provider_blocks = msg.provider_content_blocks
                if not isinstance(provider_blocks, list) or len(provider_blocks) > 128:
                    raise ValueError("Anthropic provider content blocks are invalid")
                if any(
                    not isinstance(block, dict)
                    or not isinstance(block.get("type"), str)
                    or not block.get("type")
                    for block in provider_blocks
                ):
                    raise ValueError("Anthropic provider content blocks are invalid")
                formatted_messages.append(
                    {
                        "role": "assistant",
                        "content": copy.deepcopy(provider_blocks),
                    }
                )
                continue

            m: dict[str, Any] = {"role": msg.role}
            content_parts: list[dict[str, Any]] = []

            # Handle vision content
            if msg.images and msg.role == "user":
                for img in msg.images:
                    if img.startswith("http"):
                        # Anthropic supports URL source
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": img,
                                },
                            }
                        )
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            media_type = "image/jpeg"
                            base64_data = img
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64_data,
                                },
                            }
                        )
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img,
                                },
                            }
                        )
            if msg.content:
                content_parts.append({"type": "text", "text": msg.content})

            if msg.role == "assistant" and msg.tool_calls:
                for tool_call in msg.tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    raw_arguments = function.get("arguments") or {}
                    if isinstance(raw_arguments, str):
                        invalid_arguments = False
                        try:
                            parsed_arguments = json.loads(raw_arguments) if raw_arguments else {}
                        except json.JSONDecodeError:
                            invalid_arguments = True
                            parsed_arguments = None
                        if invalid_arguments:
                            raise ValueError("Anthropic tool arguments must be valid JSON")
                    else:
                        parsed_arguments = raw_arguments
                    if not isinstance(parsed_arguments, dict):
                        raise ValueError("Anthropic tool arguments must decode to an object")
                    tool_id = str(tool_call.get("id") or "")
                    tool_name = str(function.get("name") or "")
                    if not tool_id or not tool_name:
                        raise ValueError("Anthropic tool calls require non-empty id and name")
                    content_parts.append(
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": parsed_arguments,
                        }
                    )

            m["content"] = content_parts if content_parts else str(msg.content or "")

            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
            "stream": stream,
        }
        if system_prompt:
            # Split the prompt on CACHE_SPLIT_MARKER (inserted by
            # build_system_prompt_v2) into a tenant-stable static prefix and
            # a per-tenant/per-scenario tail. Both get `cache_control:
            # ephemeral` so the prefix caches across all tenants while the
            # tail still caches per (tenant, scenario, tools) combination.
            # Anthropic allows up to 4 cache breakpoints; we use 2.
            from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

            if CACHE_SPLIT_MARKER in system_prompt:
                static_prefix, dynamic_tail = system_prompt.split(CACHE_SPLIT_MARKER, 1)
                blocks = [
                    {
                        "type": "text",
                        "text": static_prefix.rstrip(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                dynamic_tail = dynamic_tail.lstrip()
                if dynamic_tail:
                    blocks.append(
                        {
                            "type": "text",
                            "text": dynamic_tail,
                            "cache_control": {"type": "ephemeral"},
                        }
                    )
                body["system"] = blocks
            else:
                body["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
        if tools:
            # Convert OpenAI tool format to Anthropic format.
            anthropic_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    anthropic_tools.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "input_schema": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if anthropic_tools:
                # Cache the tool definitions too — they're stable across turns
                # for the same session. Put the marker on the last tool entry;
                # Anthropic caches everything up to (and including) the marker.
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
                body["tools"] = anthropic_tools

        # Native search — Anthropic server tool `web_search_20250305`. Append
        # to the tools list (the model will call it internally and return
        # inline citations as `tool_use`/`tool_result` blocks we already
        # ignore in streaming; sink it to DEBUG if it shows up).
        if native_search_config and native_search_config.get("tool_type") == "web_search_20250305":
            server_tool = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": native_search_config.get("max_uses", 5),
            }
            existing = list(body.get("tools") or [])
            existing.append(server_tool)
            body["tools"] = existing
        return body

    def _build_google_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Google Gemini API request body."""
        del stream
        contents = []
        system_instruction = None

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_instruction = msg.content
                continue

            # Handle tool result messages (functionResponse)
            if msg.role == "tool" and msg.tool_call_id:
                # Parse function name from tool_call_id (format: "call_<name>")
                func_name = msg.name or msg.tool_call_id
                if func_name.startswith("call_"):
                    func_name = func_name[5:]

                # Try to parse content as JSON, otherwise wrap as object
                try:
                    response_data = json.loads(msg.content) if msg.content else {}
                except json.JSONDecodeError:
                    response_data = {"result": msg.content}

                contents.append(
                    {
                        "role": "user",  # Google uses "user" role for function responses
                        "parts": [
                            {"functionResponse": {"name": func_name, "response": response_data}}
                        ],
                    }
                )
                continue

            role = "user" if msg.role == "user" else "model"
            parts = []

            # Handle assistant messages with tool_calls (functionCall)
            if msg.role == "assistant":
                # Add text content first if present
                if msg.content:
                    text_part = {"text": msg.content}
                    # Attach thoughtSignature to text part if present
                    if msg.thought_signature:
                        text_part["thoughtSignature"] = msg.thought_signature
                    parts.append(text_part)

                # Add function calls with thoughtSignature (CRITICAL for Gemini 3)
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        if not isinstance(tc, dict):
                            raise ValueError("Google tool call history must be an object")
                        func = tc.get("function", {})
                        if not isinstance(func, dict):
                            raise ValueError("Google tool function history must be an object")
                        func_name = func.get("name", "")
                        if not isinstance(func_name, str) or not func_name:
                            raise ValueError("Google tool function name must be non-empty")

                        # Parse arguments
                        invalid_arguments = False
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except (json.JSONDecodeError, TypeError):
                            invalid_arguments = True
                            args = None
                        if invalid_arguments:
                            raise ValueError("Google tool arguments must be valid JSON")
                        if not isinstance(args, dict):
                            raise ValueError("Google tool arguments must be an object")

                        func_call_part: dict[str, Any] = {
                            "functionCall": {"name": func_name, "args": args}
                        }

                        # CRITICAL: Include thoughtSignature if present (required for Gemini 3)
                        if "thoughtSignature" in tc:
                            func_call_part["thoughtSignature"] = tc["thoughtSignature"]
                            name_hash = hashlib.sha256(str(func_name).encode("utf-8")).hexdigest()[
                                :10
                            ]
                            logger.debug(
                                "[GEMINI3] Including thoughtSignature name_hash=%s",
                                name_hash,
                            )

                        parts.append(func_call_part)

                # If only thoughtSignature is present without content or tool calls (unlikely but possible)
                if not msg.content and not msg.tool_calls and msg.thought_signature:
                    parts.append({"text": "", "thoughtSignature": msg.thought_signature})

                if parts:
                    contents.append({"role": role, "parts": parts})
                continue

            # Handle vision content
            if msg.images and msg.role == "user":
                for img in msg.images:
                    if img.startswith("http"):
                        # Infer mime type from URL extension
                        mime_type = "image/jpeg"
                        if ".png" in img.lower():
                            mime_type = "image/png"
                        elif ".gif" in img.lower():
                            mime_type = "image/gif"
                        elif ".webp" in img.lower():
                            mime_type = "image/webp"
                        parts.append({"fileData": {"fileUri": img, "mimeType": mime_type}})
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            mime_type = "image/jpeg"
                            base64_data = img
                        # Gemini REST API uses camelCase
                        parts.append({"inlineData": {"mimeType": mime_type, "data": base64_data}})
                    else:
                        # Raw base64 data, assume jpeg
                        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": img}})
                parts.append({"text": msg.content})
            else:
                parts.append({"text": msg.content})

            contents.append({"role": role, "parts": parts})

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens or 8192,
            },
        }

        # Thinking configuration.
        #
        # Gemini 2.5+ / 3.x only emits "thought summary" parts
        # (`candidates[].content.parts[].thought == true`) when the request
        # body explicitly enables `thinkingConfig.includeThoughts`. Without
        # it the REST API silently drops thinking content, which breaks the
        # Activity drawer (no thinking_start / thinking_delta SSE events).
        #
        # Rules:
        #   - When the caller explicitly sets `thinking_level`, honour it
        #     (PPT request path) AND turn on includeThoughts so thought
        #     summaries still stream to the Activity drawer.
        #   - Otherwise, default to `includeThoughts: true` for Gemini
        #     models that support thought summaries (2.5+ / 3.x). We skip
        #     older ids (gemini-1.5-*, gemini-pro, etc.) since their REST
        #     surface does not accept the field.
        mid = (model_id or "").lower()
        supports_thought_summaries = "gemini-2.5" in mid or "gemini-3" in mid
        # Gemini 3 Flash defaults to an aggressive thinking level —
        # observed: 100+ ``thoughtsTokenCount`` even for a 2-character
        # greeting, yielding ~20s TTFT because nothing visible streams
        # during the thinking window. Bias toward ``"low"`` for Flash-tier
        # 3.x models when the caller hasn't asked for more. Users who
        # want deeper thinking opt in via the UI's thinking-level chip.
        is_gemini_3_flash = "gemini-3" in mid and "flash" in mid
        default_thinking_level = "low" if is_gemini_3_flash else None

        effective_level = thinking_level or default_thinking_level
        if effective_level:
            thinking_cfg: dict[str, Any] = {"thinkingLevel": effective_level}
            if supports_thought_summaries:
                thinking_cfg["includeThoughts"] = True
            body["generationConfig"]["thinkingConfig"] = thinking_cfg
        elif supports_thought_summaries:
            body["generationConfig"]["thinkingConfig"] = {"includeThoughts": True}

        if system_instruction:
            # Strip Anthropic-only cache marker before sending to Gemini.
            from ..prompts.system_prompt_v2 import CACHE_SPLIT_MARKER

            if isinstance(system_instruction, str) and CACHE_SPLIT_MARKER in system_instruction:
                system_instruction = system_instruction.replace(CACHE_SPLIT_MARKER, "").replace(
                    "\n\n\n\n", "\n\n"
                )
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        if tools:
            # Convert OpenAI tool format to Google format
            google_tools = []
            function_declarations = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    function_declarations.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "parameters": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if function_declarations:
                google_tools.append({"functionDeclarations": function_declarations})
                body["tools"] = google_tools

        # Native search — Gemini 3.x supports combining built-in grounding
        # (`google_search`) with `functionDeclarations` in a single request,
        # so always append. Older Gemini 1.5 / 2.0 used to 400 on this combo;
        # we no longer ship those models. The capability map tells us which
        # form to emit (`google_search` vs the legacy `google_search_retrieval`).
        if native_search_config and native_search_config.get("tool_type") in (
            "google_search",
            "google_search_retrieval",
        ):
            search_tool_key = native_search_config["tool_type"]
            existing = list(body.get("tools") or [])
            existing.append({search_tool_key: {}})
            body["tools"] = existing

        # Apply tool_config if provided
        if tool_config:
            body["toolConfig"] = tool_config

        return body

    async def chat(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, int]]:
        """
        Non-streaming chat completion.

        Returns:
            Tuple of (response_content, usage_dict)
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider, model_id=model_id)
        # Google providers return ephemeral clients from _get_client;
        # wrap the call so the client is closed even on exception.
        _owns_client = model.provider in (
            ModelProvider.GOOGLE,
            ModelProvider.GOOGLE_VERTEX,
        )
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=False,
            thinking_level=thinking_level,
            native_search_config=native_search_config,
        )

        if model.provider == ModelProvider.GOOGLE:
            # Path differs between AI Studio and Vertex; auth stays in headers.
            endpoint = self._google_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.GOOGLE_VERTEX:
            endpoint = self._vertex_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.ANTHROPIC:
            endpoint = "/v1/messages"
        else:
            endpoint = "/v1/chat/completions"

        safe_transport_error: httpx.RequestError | None = None
        try:
            try:
                response = await client.post(endpoint, json=body)
            except httpx.RequestError as exc:
                safe_transport_error = _safe_request_error(exc)
            if safe_transport_error is not None:
                raise safe_transport_error
            _raise_for_status_without_query_secrets(response)
            invalid_response = False
            try:
                data = response.json()
            except Exception:
                invalid_response = True
                data = None
            if invalid_response or not isinstance(data, dict):
                provider_label = (
                    "anthropic"
                    if model.provider == ModelProvider.ANTHROPIC
                    else (
                        "google"
                        if model.provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)
                        else "openai-compatible"
                    )
                )
                raise ProviderStreamError(provider_label, "invalid_response_json")
        finally:
            if _owns_client:
                await client.aclose()

        if model.provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            # Parse Google Gemini response
            content = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
            usage = _sanitize_usage(data.get("usageMetadata", {}))
        elif model.provider == ModelProvider.ANTHROPIC:
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            usage = _sanitize_usage(data.get("usage", {}))
        else:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = _sanitize_usage(data.get("usage", {}))

        return content, usage

    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
        native_search_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """
        Streaming chat completion.

        Yields StreamDelta objects with incremental content.
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider, model_id=model_id)
        # Google providers return ephemeral clients; close on exit so no
        # TLS connection outlives the stream.
        _owns_client = model.provider in (
            ModelProvider.GOOGLE,
            ModelProvider.GOOGLE_VERTEX,
        )
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=True,
            thinking_level=thinking_level,
            tool_config=tool_config,
            native_search_config=native_search_config,
        )

        try:
            if model.provider == ModelProvider.GOOGLE:
                endpoint = self._google_endpoint(model_id, stream=True)
                async for delta in self._stream_google(client, endpoint, body):
                    yield delta
            elif model.provider == ModelProvider.GOOGLE_VERTEX:
                endpoint = self._vertex_endpoint(model_id, stream=True)
                async for delta in self._stream_google(client, endpoint, body):
                    yield delta
            elif model.provider == ModelProvider.ANTHROPIC:
                endpoint = "/v1/messages"
                async for delta in self._stream_anthropic(client, endpoint, body):
                    yield delta
            else:
                endpoint = "/v1/chat/completions"
                async for delta in self._stream_openai(client, endpoint, body):
                    yield delta
        finally:
            if _owns_client:
                await client.aclose()

    async def _stream_openai(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from OpenAI-compatible API."""
        # Stateful <think> tag parser for models that embed thinking in content
        in_think_block = False
        think_buf = ""
        saw_tool_call = False
        saw_terminal_event = False
        terminal_reason: str | None = None

        async with _safe_provider_stream(client, endpoint, body) as response:
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    saw_terminal_event = True
                    break
                evt = _parse_sse_event(data_str, provider="openai-compatible")

                if "error" in evt:
                    error = evt.get("error")
                    error_type = error.get("type") if isinstance(error, dict) else None
                    if (
                        not isinstance(error_type, str)
                        or error_type not in _SAFE_OPENAI_ERROR_TYPES
                    ):
                        error_type = "provider_error"
                    raise ProviderStreamError("openai-compatible", error_type)

                # Handle usage - can appear in final chunk alongside choices
                usage_data = None
                if isinstance(evt.get("usage"), dict):
                    usage_data = _sanitize_usage(evt["usage"])
                    logger.debug(f"[USAGE] Received usage data: {usage_data}")

                # Safely get choices - may be empty list or missing
                choices = evt.get("choices", [])
                if not isinstance(choices, list):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                if saw_terminal_event:
                    if not choices and usage_data:
                        yield StreamDelta(usage=usage_data)
                        continue
                    raise ProviderStreamError("openai-compatible", "event_after_terminal")
                if not choices:
                    # No choices in this event, only yield if we have usage data
                    if usage_data:
                        yield StreamDelta(usage=usage_data)
                    continue

                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None and not isinstance(finish_reason, str):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                if finish_reason and finish_reason not in _SAFE_OPENAI_FINISH_REASONS:
                    raise ProviderStreamError("openai-compatible", "invalid_finish_reason")

                content = delta.get("content", "") or ""
                reasoning = delta.get("reasoning_content")
                if not isinstance(content, str) or (
                    reasoning is not None and not isinstance(reasoning, str)
                ):
                    raise ProviderStreamError("openai-compatible", "invalid_event")
                tool_calls = _validate_openai_tool_call_deltas(delta.get("tool_calls"))
                if tool_calls:
                    saw_tool_call = True
                if finish_reason:
                    saw_terminal_event = True
                    terminal_reason = finish_reason
                thinking = None

                # If provider gives reasoning_content natively, use it directly
                if reasoning:
                    thinking = reasoning
                elif content:
                    # Fallback: parse <think> tags from content stream
                    # Tags may be split across chunks, so track state
                    if in_think_block:
                        end_idx = content.find("</think>")
                        if end_idx != -1:
                            # Only yield the NEW portion from this chunk
                            thinking = content[:end_idx] if end_idx > 0 else None
                            think_buf = ""
                            in_think_block = False
                            content = content[end_idx + 8 :]  # skip </think>
                        else:
                            thinking = content
                            think_buf += content
                            content = ""
                    elif "<think>" in content:
                        start_idx = content.find("<think>")
                        pre_content = content[:start_idx]
                        rest = content[start_idx + 7 :]  # skip <think>
                        end_idx = rest.find("</think>")
                        if end_idx != -1:
                            thinking = rest[:end_idx]
                            content = pre_content + rest[end_idx + 8 :]
                        else:
                            thinking = rest
                            think_buf = rest
                            in_think_block = True
                            content = pre_content

                # SMOOTHER: DashScope Intl (and other OpenAI-compat endpoints on
                # slower networks) coalesce multiple tokens into a single SSE
                # frame — observed in prod 2026-04-24 yielding only 2 text
                # deltas for a "count 1-10" prompt, visible to users as "no
                # streaming, just one dump." Same remediation as the Google
                # path (_stream_google below): split a content chunk into
                # token-sized sub-deltas with a small inter-chunk delay so
                # the frontend renders a typewriter cadence.
                #
                # Only applies when the chunk contains visible content and no
                # side-channel payload (tool_calls / usage / thinking) —
                # those ride alone so ordering is preserved. Usage-only /
                # finish-only events are emitted unchanged.
                has_meta = bool(tool_calls or finish_reason or usage_data or thinking)
                if content and not has_meta:
                    async for _sub in _smooth_text_delta(content):
                        yield StreamDelta(content=_sub)
                else:
                    yield StreamDelta(
                        content=content,
                        tool_calls=tool_calls,
                        finish_reason=finish_reason,
                        usage=usage_data,
                        thinking_content=thinking,
                    )
        if not saw_terminal_event:
            raise ProviderStreamError("openai-compatible", "incomplete_message")
        if saw_tool_call and terminal_reason not in {"tool_calls", "function_call"}:
            raise ProviderStreamError("openai-compatible", "incomplete_tool_call")

    async def _stream_anthropic(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Anthropic API."""
        tool_blocks: dict[int, dict[str, str]] = {}
        input_buffers: dict[int, str] = {}
        open_blocks: dict[int, str] = {}
        provider_blocks: dict[int, dict[str, Any]] = {}
        provider_block_order: list[int] = []
        saw_tool_call = False
        message_started = False
        message_stopped = False
        message_delta_started = False
        terminal_reason: str | None = None
        terminal_usage: dict[str, int] | None = None
        lifecycle_events = {
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        }

        def event_index(event: dict[str, Any]) -> int:
            raw_index = event.get("index")
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ProviderStreamError("anthropic", "invalid_event")
            return raw_index

        async with _safe_provider_stream(client, endpoint, body) as response:
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                evt = _parse_sse_event(data_str, provider="anthropic")
                evt_type = evt.get("type")
                if not isinstance(evt_type, str):
                    raise ProviderStreamError("anthropic", "invalid_event")

                if evt_type == "error":
                    error = evt.get("error")
                    error_type = (
                        str(error.get("type") or "provider_error")
                        if isinstance(error, dict)
                        else "provider_error"
                    )
                    if error_type not in _SAFE_ANTHROPIC_ERROR_TYPES:
                        error_type = "provider_error"
                    raise ProviderStreamError("anthropic", error_type)

                # Anthropic permits pings anywhere in the stream, including
                # between a terminal message_delta and message_stop.
                if evt_type == "ping":
                    continue

                # The versioning contract permits new event types. Ignore
                # unknown typed events while keeping known lifecycle events
                # strictly ordered and paired.
                if evt_type not in lifecycle_events:
                    continue

                if not message_started:
                    if evt_type != "message_start":
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    message_started = True
                    message = evt.get("message")
                    if not isinstance(message, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    raw_usage = message.get("usage", {})
                    if not isinstance(raw_usage, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    usage = _sanitize_usage(raw_usage)
                    if usage:
                        yield StreamDelta(usage=usage)
                    continue

                if message_delta_started:
                    if evt_type == "message_stop":
                        if open_blocks:
                            raise ProviderStreamError("anthropic", "incomplete_content_block")
                        if terminal_reason is None:
                            raise ProviderStreamError("anthropic", "incomplete_message")
                        message_stopped = True
                        yield StreamDelta(
                            finish_reason=terminal_reason,
                            usage=terminal_usage,
                            provider_content_blocks=[
                                copy.deepcopy(provider_blocks[index])
                                for index in provider_block_order
                            ],
                        )
                        break
                    if evt_type != "message_delta":
                        raise ProviderStreamError("anthropic", "event_after_terminal")

                if evt_type in {"message_start", "message_stop"}:
                    raise ProviderStreamError("anthropic", "invalid_event_order")

                if evt_type == "content_block_start":
                    index = event_index(evt)
                    if index in provider_blocks:
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    block = evt.get("content_block")
                    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    block_type = block["type"]
                    if not block_type:
                        raise ProviderStreamError("anthropic", "invalid_event")
                    open_blocks[index] = block_type
                    provider_blocks[index] = copy.deepcopy(block)
                    provider_block_order.append(index)
                    if block_type in {"tool_use", "server_tool_use"}:
                        tool_id = block.get("id")
                        tool_name = block.get("name")
                        if (
                            not isinstance(tool_id, str)
                            or not tool_id
                            or not isinstance(tool_name, str)
                            or not tool_name
                        ):
                            raise ProviderStreamError("anthropic", "invalid_tool_use")
                        initial_input = block.get("input")
                        initial_arguments = ""
                        if initial_input not in (None, {}):
                            if not isinstance(initial_input, dict):
                                raise ProviderStreamError("anthropic", "invalid_tool_input")
                            initial_arguments = json.dumps(
                                initial_input,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        input_buffers[index] = initial_arguments
                        if block_type == "server_tool_use":
                            continue
                        saw_tool_call = True
                        tool_blocks[index] = {
                            "id": tool_id,
                            "name": tool_name,
                            "arguments": initial_arguments,
                        }
                        yield StreamDelta(
                            tool_calls=[
                                {
                                    "index": index,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": initial_arguments,
                                    },
                                }
                            ]
                        )
                    continue

                if evt_type == "content_block_delta":
                    index = event_index(evt)
                    block_type = open_blocks.get(index)
                    if block_type is None:
                        raise ProviderStreamError("anthropic", "orphan_content_block_delta")
                    delta = evt.get("delta")
                    if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    delta_type = delta["type"]
                    if block_type == "tool_use" and delta_type != "input_json_delta":
                        raise ProviderStreamError("anthropic", "invalid_tool_input")
                    if delta_type == "text_delta" and block_type != "text":
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "input_json_delta" and block_type not in {
                        "tool_use",
                        "server_tool_use",
                    }:
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type in {"thinking_delta", "signature_delta"} and block_type != (
                        "thinking"
                    ):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "citations_delta" and block_type != "text":
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if delta_type == "text_delta":
                        text = delta.get("text", "")
                        if not isinstance(text, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block = provider_blocks[index]
                        existing_text = provider_block.get("text", "")
                        if not isinstance(existing_text, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block["text"] = existing_text + text
                        yield StreamDelta(content=text)
                    elif delta_type == "input_json_delta" and block_type in {
                        "tool_use",
                        "server_tool_use",
                    }:
                        partial_json = delta.get("partial_json", "")
                        if not isinstance(partial_json, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        input_buffers[index] = input_buffers.get(index, "") + partial_json
                        block = tool_blocks.get(index)
                        if block is not None:
                            block["arguments"] += partial_json
                        if partial_json and block_type == "tool_use":
                            yield StreamDelta(
                                tool_calls=[
                                    {
                                        "index": index,
                                        "function": {"arguments": partial_json},
                                    }
                                ]
                            )
                    elif block_type == "tool_use":
                        # Unknown deltas on a client-executable tool must fail
                        # closed. Ignoring them could turn malformed arguments
                        # into an executable empty object.
                        raise ProviderStreamError("anthropic", "invalid_tool_input")
                    elif delta_type == "thinking_delta":
                        thinking = delta.get("thinking", "")
                        if not isinstance(thinking, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block = provider_blocks[index]
                        existing = provider_block.get("thinking", "")
                        if not isinstance(existing, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_block["thinking"] = existing + thinking
                        yield StreamDelta(thinking_content=thinking)
                    elif delta_type == "signature_delta":
                        signature = delta.get("signature")
                        if not isinstance(signature, str):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        provider_blocks[index]["signature"] = signature
                    elif delta_type == "citations_delta":
                        citation = delta.get("citation")
                        if block_type != "text" or not isinstance(citation, dict):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        citations = provider_blocks[index].setdefault("citations", [])
                        if not isinstance(citations, list):
                            raise ProviderStreamError("anthropic", "invalid_event")
                        citations.append(copy.deepcopy(citation))

                elif evt_type == "content_block_stop":
                    index = event_index(evt)
                    block_type = open_blocks.pop(index, None)
                    if block_type is None:
                        raise ProviderStreamError("anthropic", "invalid_event_order")
                    block = tool_blocks.pop(index, None)
                    if block_type in {"tool_use", "server_tool_use"}:
                        if block_type == "tool_use" and block is None:
                            raise ProviderStreamError("anthropic", "incomplete_tool_use")
                        invalid_arguments = False
                        try:
                            parsed_arguments = json.loads(input_buffers.pop(index, "") or "{}")
                        except json.JSONDecodeError:
                            invalid_arguments = True
                            parsed_arguments = None
                        if invalid_arguments:
                            raise ProviderStreamError("anthropic", "invalid_tool_input_json")
                        if not isinstance(parsed_arguments, dict):
                            raise ProviderStreamError("anthropic", "invalid_tool_input")
                        provider_blocks[index]["input"] = parsed_arguments

                elif evt_type == "message_delta":
                    if open_blocks:
                        raise ProviderStreamError("anthropic", "incomplete_content_block")
                    message_delta_started = True
                    delta = evt.get("delta")
                    if not isinstance(delta, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    stop_reason = delta.get("stop_reason")
                    if stop_reason is not None and not isinstance(stop_reason, str):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    if stop_reason:
                        if stop_reason not in _SAFE_ANTHROPIC_STOP_REASONS:
                            raise ProviderStreamError("anthropic", "invalid_stop_reason")
                        if terminal_reason is not None and terminal_reason != stop_reason:
                            raise ProviderStreamError("anthropic", "invalid_event_order")
                        terminal_reason = stop_reason
                    raw_usage = evt.get("usage", {})
                    if not isinstance(raw_usage, dict):
                        raise ProviderStreamError("anthropic", "invalid_event")
                    usage = _sanitize_usage(raw_usage)
                    if usage:
                        terminal_usage = usage
                    yield StreamDelta(
                        finish_reason=stop_reason,
                        usage=usage or None,
                    )

            if tool_blocks:
                raise ProviderStreamError("anthropic", "incomplete_tool_use")
            if open_blocks:
                raise ProviderStreamError("anthropic", "incomplete_content_block")
            if not message_started or not message_stopped:
                raise ProviderStreamError("anthropic", "incomplete_message")
            if saw_tool_call and terminal_reason != "tool_use":
                raise ProviderStreamError("anthropic", "incomplete_tool_call")

    async def _stream_google(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Google Gemini API."""
        tool_count = sum(
            len(tool.get("functionDeclarations") or [])
            for tool in body.get("tools", [])
            if isinstance(tool, dict)
        )
        content_count = len(body.get("contents") or [])
        logger.info(
            "[GEMINI] request prepared: contents=%s tools=%s system=%s",
            content_count,
            tool_count,
            bool(body.get("systemInstruction")),
        )

        # Wire-level timing — helps diagnose whether a slow response is
        # client-side (context/tool-prep), network-side (httpx connect/TLS),
        # or server-side (model inference). Each phase is logged at INFO
        # so we don't need to flip debug levels in prod to debug latency.
        import time as _time

        _request_started = _time.perf_counter()
        _host = "unknown"
        safe_transport_error: httpx.RequestError | None = None
        try:
            from urllib.parse import urlparse as _urlparse

            _host = _urlparse(endpoint).hostname or "unknown"
        except Exception:
            pass
        logger.info(f"[GEMINI] HTTP POST → host={_host}")

        # Track functionCall parts already emitted in this stream to avoid
        # duplicate tool calls. Gemini streaming does not provide stable tool
        # call ids, and the same functionCall part can legitimately appear in
        # more than one SSE data frame (e.g. once in the content chunk, once
        # in the finish chunk). Since we synthesize a fresh uuid per part,
        # naive emission creates duplicate tool_call pills in the Activity
        # drawer. Key on (name, args_json) — thoughtSignature varies so we
        # don't include it in the dedup key.
        emitted_function_calls: set[tuple[str, str]] = set()

        try:
            stream_context = client.stream("POST", endpoint, json=body)
            async with stream_context as response:
                _headers_ms = (_time.perf_counter() - _request_started) * 1000
                logger.info(
                    f"[GEMINI] response headers received after {_headers_ms:.0f}ms "
                    f"(host={_host} status={response.status_code})"
                )
                if not 200 <= response.status_code < 300:
                    error_body = await response.aread()
                    request_id = (
                        response.headers.get("x-request-id")
                        or response.headers.get("x-goog-request-id")
                        or "unknown"
                    )
                    logger.error(
                        "[GEMINI] provider error: status=%s host=%s request_id=%s body_bytes=%s",
                        response.status_code,
                        _host,
                        request_id,
                        len(error_body),
                    )
                _raise_for_status_without_query_secrets(response)
                async for delta in self._consume_google_stream(
                    response,
                    request_started=_request_started,
                    host=_host,
                    emitted_function_calls=emitted_function_calls,
                ):
                    yield delta
        except httpx.HTTPStatusError:
            raise
        except httpx.RequestError as exc:
            safe_transport_error = _safe_request_error(exc)
        if safe_transport_error is not None:
            raise safe_transport_error

    async def _consume_google_stream(
        self,
        response: httpx.Response,
        *,
        request_started: float,
        host: str,
        emitted_function_calls: set[tuple[str, str]],
    ) -> AsyncIterator[StreamDelta]:
        """Consume a successful Gemini stream without retaining request secrets."""
        import time as _time

        _request_started = request_started
        _host = host
        _first_line_logged = False
        saw_tool_call = False
        saw_terminal_event = False
        terminal_reason: str | None = None
        async for line in response.aiter_lines():
            if line is not None:
                if not _first_line_logged:
                    _first_line_ms = (_time.perf_counter() - _request_started) * 1000
                    logger.info(
                        f"[GEMINI] first SSE line after {_first_line_ms:.0f}ms (host={_host})"
                    )
                    _first_line_logged = True
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                evt = _parse_sse_event(data_str, provider="google")
                candidates = evt.get("candidates", [])
                usage_meta = evt.get("usageMetadata", {})
                prompt_feedback = evt.get("promptFeedback")
                if not isinstance(candidates, list) or not isinstance(usage_meta, dict):
                    raise ProviderStreamError("google", "invalid_event")
                if prompt_feedback is not None and not isinstance(prompt_feedback, dict):
                    raise ProviderStreamError("google", "invalid_event")

                if saw_terminal_event:
                    if not candidates and usage_meta:
                        usage = _sanitize_usage(usage_meta)
                        if usage:
                            yield StreamDelta(usage=usage)
                        continue
                    raise ProviderStreamError("google", "event_after_terminal")

                # Check for promptFeedback (Safety blocking)
                if prompt_feedback and prompt_feedback.get("blockReason"):
                    raw_block_reason = str(prompt_feedback.get("blockReason") or "")
                    block_reason = (
                        raw_block_reason
                        if raw_block_reason in _SAFE_GOOGLE_BLOCK_REASONS
                        else "UNKNOWN"
                    )
                    logger.warning("[GEMINI] Response blocked: %s", block_reason)
                    saw_terminal_event = True
                    terminal_reason = "SAFETY"
                    yield StreamDelta(
                        finish_reason="safety",
                        content=f"\n\n[System: Response blocked due to safety reason: {block_reason}]",
                    )
                    continue

                if candidates:
                    candidate = candidates[0]
                    if not isinstance(candidate, dict):
                        raise ProviderStreamError("google", "invalid_event")
                    content = candidate.get("content", {})
                    if not isinstance(content, dict):
                        raise ProviderStreamError("google", "invalid_event")
                    parts = content.get("parts", [])
                    if not isinstance(parts, list):
                        raise ProviderStreamError("google", "invalid_event")

                    # Native search grounding — log chunks/citations at DEBUG.
                    # Frontend relies on inline markdown links for now; we
                    # don't emit a new SSE event in this first round.
                    grounding = candidate.get("groundingMetadata")
                    if grounding:
                        if not isinstance(grounding, dict):
                            raise ProviderStreamError("google", "invalid_event")
                        chunks = grounding.get("groundingChunks") or []
                        if not isinstance(chunks, list):
                            raise ProviderStreamError("google", "invalid_event")
                        logger.debug(f"[GEMINI] groundingChunks received: {len(chunks)} entries")

                    # Check for finishReason in candidate even if parts are empty
                    finish_reason = candidate.get("finishReason")
                    if finish_reason is not None and not isinstance(finish_reason, str):
                        raise ProviderStreamError("google", "invalid_event")
                    if finish_reason:
                        if finish_reason.upper() not in _SAFE_GOOGLE_FINISH_REASONS:
                            raise ProviderStreamError("google", "invalid_finish_reason")
                        saw_terminal_event = True
                        terminal_reason = finish_reason.upper()

                    if not parts and finish_reason:
                        # Handle case where only finishReason is sent (e.g. SAFETY, STOP)
                        yield StreamDelta(finish_reason=finish_reason.lower())

                    tool_calls_batch = []
                    for part in parts:
                        if not isinstance(part, dict):
                            raise ProviderStreamError("google", "invalid_event")
                        if part.get("thought") and "text" in part:
                            # Gemini 3 thinking content (thought parts).
                            # Guard against a Gemini quirk where thought text
                            # arrives with literal ``\n`` escape sequences
                            # instead of real newlines. Thought summaries are
                            # natural-language prose — legitimate occurrences
                            # of literal ``\n`` don't happen — so when we see
                            # ``\n`` strings AND no real newlines, unescape.
                            _thought_text = part["text"]
                            if not isinstance(_thought_text, str):
                                raise ProviderStreamError("google", "invalid_event")
                            if "\\n" in _thought_text and "\n" not in _thought_text:
                                with contextlib.suppress(
                                    UnicodeDecodeError,
                                    UnicodeEncodeError,
                                ):
                                    _thought_text = _thought_text.encode("utf-8").decode(
                                        "unicode_escape"
                                    )
                            yield StreamDelta(thinking_content=_thought_text)
                        elif "text" in part:
                            # SMOOTHER: Vertex Express Mode streams ~1 SSE frame
                            # per second with ~100 chars at a time (verified with
                            # direct curl against aiplatform.googleapis.com —
                            # list-30-facts yielded only 6 SSE events over 6.5s).
                            # Emitting that as a single StreamDelta makes the
                            # frontend render in 3-6 large bursts, which users
                            # perceive as "not streaming." Split each Vertex
                            # frame into smaller deltas with a small inter-chunk
                            # delay so the frontend sees token-like cadence.
                            _text = part["text"]
                            if not isinstance(_text, str):
                                raise ProviderStreamError("google", "invalid_event")
                            async for _sub in _smooth_text_delta(_text):
                                yield StreamDelta(content=_sub)
                        elif "functionCall" in part:
                            saw_tool_call = True
                            # Gemini 3 function call with optional thoughtSignature
                            fc = part["functionCall"]
                            # IMPORTANT: tool_call ids must be unique per call for the assistant UI.
                            # Gemini streaming does not provide a stable unique call id, so we generate one.
                            import uuid

                            if not isinstance(fc, dict):
                                raise ProviderStreamError("google", "invalid_event")
                            fc_name = fc.get("name") or "unknown"
                            fc_args = fc.get("args", {})
                            if not isinstance(fc_name, str) or not isinstance(fc_args, dict):
                                raise ProviderStreamError("google", "invalid_event")
                            fc_args_json = json.dumps(fc_args, sort_keys=True, ensure_ascii=False)
                            dedup_key = (str(fc_name), fc_args_json)
                            # args_hash helps diagnose duplicate-pill
                            # regressions (lets us see whether re-emitted
                            # chunks carry identical args or varied ones).
                            # Emit stays DEBUG to keep prod log volume sane;
                            # SKIP stays INFO — each skip is an actual signal.
                            _args_hash = hashlib.sha256(fc_args_json.encode("utf-8")).hexdigest()[
                                :10
                            ]
                            _name_hash = hashlib.sha256(str(fc_name).encode("utf-8")).hexdigest()[
                                :10
                            ]
                            if dedup_key in emitted_function_calls:
                                # Provider re-emitted the same functionCall in
                                # a later SSE chunk. Skip — downstream already
                                # accumulated it under a fresh uuid, and
                                # adding another copy would create a duplicate
                                # Activity-drawer pill.
                                logger.info(
                                    f"[GEMINI] functionCall SKIP (dup): "
                                    f"name_hash={_name_hash} args_hash={_args_hash}"
                                )
                                continue
                            emitted_function_calls.add(dedup_key)
                            logger.debug(
                                f"[GEMINI] functionCall emit: "
                                f"name_hash={_name_hash} args_hash={_args_hash}"
                            )

                            tool_call: dict[str, Any] = {
                                "id": f"call_{fc_name}_{uuid.uuid4().hex[:10]}",
                                "type": "function",
                                "function": {
                                    "name": fc_name,
                                    "arguments": fc_args_json,
                                },
                            }
                            # CRITICAL: Preserve thoughtSignature for Gemini 3
                            # This must be passed back in subsequent requests
                            if "thoughtSignature" in part:
                                tool_call["thoughtSignature"] = part["thoughtSignature"]
                                logger.debug("[GEMINI3] Captured thoughtSignature for tool call")
                            tool_calls_batch.append(tool_call)

                        # Capture standalone thoughtSignature if present (rare but possible)
                        elif "thoughtSignature" in part and "functionCall" not in part:
                            logger.debug("[GEMINI3] Captured standalone thoughtSignature")
                            ts = part["thoughtSignature"]
                            if not isinstance(ts, str):
                                raise ProviderStreamError("google", "invalid_event")
                            # If we have text content in the same part (which shouldn't happen based on API structure, but to be safe)
                            # Or if we want to yield it attached to text
                            yield StreamDelta(thought_signature=ts)

                    # Yield all tool calls together
                    if tool_calls_batch:
                        yield StreamDelta(tool_calls=tool_calls_batch)

                    # Check finish reason
                    if finish_reason:
                        yield StreamDelta(finish_reason=finish_reason.lower())

                else:
                    # No candidates - could be usage metadata only or keep-alive
                    pass

                # Handle usage metadata
                usage = _sanitize_usage(usage_meta) if usage_meta else {}
                if usage:
                    yield StreamDelta(usage=usage)
        if not saw_terminal_event:
            raise ProviderStreamError("google", "incomplete_message")
        if saw_tool_call and terminal_reason != "STOP":
            raise ProviderStreamError("google", "incomplete_tool_call")

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
