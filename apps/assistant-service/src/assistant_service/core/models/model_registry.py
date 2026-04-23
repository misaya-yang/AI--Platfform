"""
Model Registry - Unified interface for multiple LLM providers.

Supports (default catalog as of 2026-04):
- OpenAI (gpt-4o, o1)
- Anthropic (claude-opus-4-5, claude-sonnet-4-5)
- DeepSeek (deepseek-chat, deepseek-reasoner)
- DashScope/Qwen (qwen3.6-plus, qwen-max)
- Google / Google Vertex (gemini-3-pro-preview, gemini-3-flash-preview)
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from ai_gateway_core.logging import get_logger

logger = get_logger(__name__)


class ModelProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    DASHSCOPE = "dashscope"
    GOOGLE = "google"
    # Vertex AI — same wire protocol as Google Gemini (``_build_google_body``
    # emits an identical body), only the host + path prefix differ. Kept as
    # its own enum value so operators can add it through the Provider UI the
    # same way they add any other provider, with its own API key, its own
    # DB row, and its own set of models. The legacy env-driven flip
    # (``GOOGLE_API_BACKEND=vertex``) still works but logs a deprecation
    # warning at startup — prefer configuring the ``google-vertex`` provider.
    GOOGLE_VERTEX = "google-vertex"


class ModelAccessLevel(str, Enum):
    """Model access permission levels."""

    PUBLIC = "public"  # Available to all authenticated users
    PREMIUM = "premium"  # Available to premium/paid users only
    ADMIN = "admin"  # Admin-only models (expensive or experimental)


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
    (ModelProvider.ANTHROPIC, "claude-opus-4-5"): {
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
# dependency-free; the model can still call search_web as a tool when needed.
_SEARCH_HINT_KEYWORDS = (
    # English
    "search", "latest", "news", "today", "current", "recent",
    "who is", "what is happening", "stock price", "weather",
    # Chinese
    "搜索", "查一下", "查询", "最新", "今天", "新闻", "现在", "最近",
    # Arabic
    "ابحث", "أخبار", "اليوم", "الآن",
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

    - Only include integer values (filter out nested dicts)
    - Normalize OpenAI keys (prompt_tokens -> input_tokens, completion_tokens -> output_tokens)

    Some providers (e.g., DashScope) return nested dicts like:
    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    """
    result: dict[str, int] = {}
    for k, v in raw_usage.items():
        if not isinstance(v, int):
            continue
        # Normalize OpenAI-style keys to standard format
        if k == "prompt_tokens":
            result["input_tokens"] = v
        elif k == "completion_tokens":
            result["output_tokens"] = v
        else:
            result[k] = v
    return result


@dataclass
class StreamDelta:
    """A single streaming delta from the model."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    thought_signature: str | None = None  # Gemini 3 thought signature
    thinking_content: str | None = None  # Qwen reasoning_content / Gemini thought parts


@dataclass
class ChatMessage:
    """A chat message."""

    role: str  # system, user, assistant, tool
    content: str
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    images: list[str] | None = None  # Base64 or URLs for vision models
    thought_signature: str | None = None  # Gemini 3 thought signature

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        """Create ChatMessage from dictionary."""
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            name=data.get("name"),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            images=data.get("images"),
            thought_signature=data.get("thought_signature"),
        )


def _normalize_message(msg) -> ChatMessage:
    """Convert message to ChatMessage if it's a dict."""
    if isinstance(msg, ChatMessage):
        return msg
    elif isinstance(msg, dict):
        return ChatMessage.from_dict(msg)
    else:
        raise TypeError(f"Expected ChatMessage or dict, got {type(msg)}")


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
# inside ``_google_endpoint`` so they don't require reconfiguring anything.


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
                except Exception as e:
                    logger.warning(f"Failed to load model {row.get('model_id')}: {e}")

            self._db_models_loaded = True
            logger.info(f"Loaded {loaded_count} models from database")
            return loaded_count

        except Exception as e:
            logger.warning(f"Failed to load models from database: {e}")
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
        if vertex_models_env:
            if model_id in {m.strip() for m in vertex_models_env.split(",") if m.strip()}:
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

        Vertex Express Mode accepts the same ``?key=`` query param as AI
        Studio, so we don't need OAuth / service-account flow here.

        Key selection mirrors backend selection: when a model routes to
        Vertex (either by global default or the ``GOOGLE_VERTEX_MODELS``
        env override) and ``VERTEX_API_KEY`` is set, use it in place of
        the config's api_key — the two keys are different formats
        (``AIzaSy...`` vs ``AQ.xxx``) and the wrong one against the wrong
        endpoint fails auth. Fall back to the config key only if
        ``VERTEX_API_KEY`` is unset (caller made a conscious choice).
        """
        config = self._configs.get(ModelProvider.GOOGLE)
        default_api_key = config.api_key if config else ""
        backend = self._google_backend_for_model(model_id)
        action = "streamGenerateContent" if stream else "generateContent"
        if backend == "vertex":
            base = self.VERTEX_BASE_URL
            api_key = os.environ.get("VERTEX_API_KEY", "").strip() or default_api_key
            path = f"/v1/publishers/google/models/{model_id}:{action}?key={api_key}"
        else:
            base = self.DEFAULT_BASE_URLS[ModelProvider.GOOGLE]
            api_key = default_api_key
            path = f"/v1beta/models/{model_id}:{action}?key={api_key}"
        if stream:
            path += "&alt=sse"
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
        api_key = config.api_key if config else ""
        base = (config.base_url if config else None) or self.VERTEX_BASE_URL
        upstream_id = model_id[:-len("-vertex")] if model_id.endswith("-vertex") else model_id
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1/publishers/google/models/{upstream_id}:{action}?key={api_key}"
        if stream:
            path += "&alt=sse"
        return f"{base}{path}"

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

    async def _get_client(self, provider: ModelProvider) -> httpx.AsyncClient:
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

        headers = self._build_headers(provider, config.api_key)
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
            # Google / Vertex use API key in URL (?key=...), not header.
            return {
                "Content-Type": "application/json",
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
                model_id, messages, temperature, max_tokens, tools, stream,
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
                model_id, messages, temperature, max_tokens, tools, stream,
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
            if not body.get("max_tokens") or body["max_tokens"] < 16384:
                body["max_tokens"] = 16384
        if native_search_config and native_search_config.get("enable_search"):
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

            m: dict[str, Any] = {"role": msg.role}

            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = []
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
                content_parts.append({"type": "text", "text": msg.content})
                m["content"] = content_parts
            else:
                m["content"] = msg.content

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
                static_prefix, dynamic_tail = system_prompt.split(
                    CACHE_SPLIT_MARKER, 1
                )
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
                        func = tc.get("function", {})
                        func_name = func.get("name", "")

                        # Parse arguments
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}

                        func_call_part: dict[str, Any] = {
                            "functionCall": {"name": func_name, "args": args}
                        }

                        # CRITICAL: Include thoughtSignature if present (required for Gemini 3)
                        if "thoughtSignature" in tc:
                            func_call_part["thoughtSignature"] = tc["thoughtSignature"]
                            logger.debug(f"[GEMINI3] Including thoughtSignature for {func_name}")

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
        supports_thought_summaries = (
            "gemini-2.5" in mid
            or "gemini-3" in mid
        )
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
                system_instruction = system_instruction.replace(
                    CACHE_SPLIT_MARKER, ""
                ).replace("\n\n\n\n", "\n\n")
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

        # Native search — Gemini 2.x+ exposes `google_search` as a tool; 1.5
        # uses the older `google_search_retrieval`. The capability map tells
        # us which form to emit. Append as a separate tool entry (Gemini
        # accepts multiple tool entries in one request).
        #
        # HARD CONSTRAINT: Gemini's built-in grounding tools CANNOT coexist
        # with user `functionDeclarations` in a single request — the API
        # returns 400. Callers upstream already suppress native_search_config
        # for Google when function tools are in scope, but guard here too:
        # if functionDeclarations are present, silently drop the grounding
        # tool rather than produce an un-sendable request.
        if native_search_config and native_search_config.get("tool_type") in (
            "google_search", "google_search_retrieval"
        ):
            has_function_decls = any(
                bool(t.get("functionDeclarations"))
                for t in (body.get("tools") or [])
            )
            if has_function_decls:
                logger.info(
                    "[GEMINI] Dropping native %s tool — cannot coexist with "
                    "functionDeclarations in one request.",
                    native_search_config["tool_type"],
                )
            else:
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

        client = await self._get_client(model.provider)
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
            # Path differs between AI Studio and Vertex; key comes in as a query param.
            endpoint = self._google_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.GOOGLE_VERTEX:
            endpoint = self._vertex_endpoint(model_id, stream=False)
        elif model.provider == ModelProvider.ANTHROPIC:
            endpoint = "/v1/messages"
        else:
            endpoint = "/v1/chat/completions"

        try:
            response = await client.post(endpoint, json=body)
            response.raise_for_status()
            data = response.json()
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
            usage_meta = data.get("usageMetadata", {})
            usage = {
                "input_tokens": usage_meta.get("promptTokenCount", 0),
                "output_tokens": usage_meta.get("candidatesTokenCount", 0),
            }
        elif model.provider == ModelProvider.ANTHROPIC:
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            usage = {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
            }
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

        client = await self._get_client(model.provider)
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

        async with client.stream("POST", endpoint, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Handle usage - can appear in final chunk alongside choices
                usage_data = None
                if isinstance(evt.get("usage"), dict):
                    usage_data = _sanitize_usage(evt["usage"])
                    logger.debug(f"[USAGE] Received usage data: {usage_data}")

                # Safely get choices - may be empty list or missing
                choices = evt.get("choices", [])
                if not choices:
                    # No choices in this event, only yield if we have usage data
                    if usage_data:
                        yield StreamDelta(usage=usage_data)
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason")

                content = delta.get("content", "") or ""
                reasoning = delta.get("reasoning_content")
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
                            content = content[end_idx + 8:]  # skip </think>
                        else:
                            thinking = content
                            think_buf += content
                            content = ""
                    elif "<think>" in content:
                        start_idx = content.find("<think>")
                        pre_content = content[:start_idx]
                        rest = content[start_idx + 7:]  # skip <think>
                        end_idx = rest.find("</think>")
                        if end_idx != -1:
                            thinking = rest[:end_idx]
                            content = pre_content + rest[end_idx + 8:]
                        else:
                            thinking = rest
                            think_buf = rest
                            in_think_block = True
                            content = pre_content

                yield StreamDelta(
                    content=content,
                    tool_calls=delta.get("tool_calls"),
                    finish_reason=finish_reason,
                    usage=usage_data,
                    thinking_content=thinking,
                )

    async def _stream_anthropic(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Anthropic API."""
        async with client.stream("POST", endpoint, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type")

                if evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield StreamDelta(content=delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta":
                        # Tool call arguments
                        pass

                elif evt_type == "message_delta":
                    usage = evt.get("usage", {})
                    yield StreamDelta(
                        finish_reason=evt.get("delta", {}).get("stop_reason"),
                        usage={
                            "output_tokens": usage.get("output_tokens", 0),
                        },
                    )

                elif evt_type == "message_start":
                    usage = evt.get("message", {}).get("usage", {})
                    if usage.get("input_tokens"):
                        yield StreamDelta(usage={"input_tokens": usage["input_tokens"]})

    async def _stream_google(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Google Gemini API."""
        # Debug: Log request body for troubleshooting
        import json as json_module

        tool_names = []
        for t in body.get("tools", []):
            for fd in t.get("functionDeclarations", []):
                tool_names.append(fd.get("name", "unknown"))
        logger.info(f"[GEMINI] Tools in request: {tool_names}")
        # TEMP: dump full body to /tmp so we can diff it against a known-fast
        # curl payload. Drop once the 47s Vertex latency is understood.
        try:
            import os as _os
            import tempfile as _tempfile
            _body_json = json_module.dumps(body, ensure_ascii=False, default=str)
            _body_path = _os.path.join(_tempfile.gettempdir(), "gemini_last_body.json")
            with open(_body_path, "w", encoding="utf-8") as _fh:
                _fh.write(_body_json)
            logger.info(f"[GEMINI] body bytes={len(_body_json)} dumped to {_body_path}")
        except Exception:
            pass
        logger.debug(
            f"[GEMINI] Request body: {json_module.dumps(body, ensure_ascii=False, default=str)[:2000]}"
        )

        # Wire-level timing — helps diagnose whether a slow response is
        # client-side (context/tool-prep), network-side (httpx connect/TLS),
        # or server-side (model inference). Each phase is logged at INFO
        # so we don't need to flip debug levels in prod to debug latency.
        import time as _time
        _request_started = _time.perf_counter()
        _host = "unknown"
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

        async with client.stream("POST", endpoint, json=body) as response:
            _headers_ms = (_time.perf_counter() - _request_started) * 1000
            logger.info(
                f"[GEMINI] response headers received after {_headers_ms:.0f}ms "
                f"(host={_host} status={response.status_code})"
            )
            if response.status_code != 200:
                # Read error response body for debugging
                error_body = await response.aread()
                logger.error(
                    f"[GEMINI] Error response ({response.status_code}): {error_body.decode('utf-8', errors='replace')}"
                )
            response.raise_for_status()
            _first_line_logged = False
            async for line in response.aiter_lines():
                if not _first_line_logged:
                    _first_line_ms = (_time.perf_counter() - _request_started) * 1000
                    logger.info(
                        f"[GEMINI] first SSE line after {_first_line_ms:.0f}ms "
                        f"(host={_host})"
                    )
                    _first_line_logged = True
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Parse Google Gemini streaming response
                candidates = evt.get("candidates", [])

                # Check for promptFeedback (Safety blocking)
                prompt_feedback = evt.get("promptFeedback")
                if prompt_feedback and prompt_feedback.get("blockReason"):
                    block_reason = prompt_feedback.get("blockReason")
                    logger.warning(f"[GEMINI] Response blocked: {block_reason}")
                    yield StreamDelta(
                        finish_reason="safety",
                        content=f"\n\n[System: Response blocked due to safety reason: {block_reason}]",
                    )
                    continue

                if candidates:
                    candidate = candidates[0]
                    content = candidate.get("content", {})
                    parts = content.get("parts", [])

                    # Native search grounding — log chunks/citations at DEBUG.
                    # Frontend relies on inline markdown links for now; we
                    # don't emit a new SSE event in this first round.
                    grounding = candidate.get("groundingMetadata")
                    if grounding:
                        chunks = grounding.get("groundingChunks") or []
                        logger.debug(
                            f"[GEMINI] groundingChunks received: {len(chunks)} entries"
                        )

                    # Check for finishReason in candidate even if parts are empty
                    finish_reason = candidate.get("finishReason")

                    if not parts and finish_reason:
                        # Handle case where only finishReason is sent (e.g. SAFETY, STOP)
                        yield StreamDelta(finish_reason=finish_reason.lower())

                    tool_calls_batch = []
                    for part in parts:
                        if part.get("thought") and "text" in part:
                            # Gemini 3 thinking content (thought parts).
                            # Guard against a Gemini quirk where thought text
                            # arrives with literal ``\n`` escape sequences
                            # instead of real newlines. Thought summaries are
                            # natural-language prose — legitimate occurrences
                            # of literal ``\n`` don't happen — so when we see
                            # ``\n`` strings AND no real newlines, unescape.
                            _thought_text = part["text"]
                            if "\\n" in _thought_text and "\n" not in _thought_text:
                                try:
                                    _thought_text = _thought_text.encode("utf-8").decode("unicode_escape")
                                except (UnicodeDecodeError, UnicodeEncodeError):
                                    pass  # fall through to raw text
                            yield StreamDelta(thinking_content=_thought_text)
                        elif "text" in part:
                            yield StreamDelta(content=part["text"])
                        elif "functionCall" in part:
                            # Gemini 3 function call with optional thoughtSignature
                            fc = part["functionCall"]
                            # IMPORTANT: tool_call ids must be unique per call for the assistant UI.
                            # Gemini streaming does not provide a stable unique call id, so we generate one.
                            import uuid

                            fc_name = fc.get("name") or "unknown"
                            fc_args_json = json.dumps(
                                fc.get("args", {}), sort_keys=True, ensure_ascii=False
                            )
                            dedup_key = (str(fc_name), fc_args_json)
                            # args_hash helps diagnose duplicate-pill
                            # regressions (lets us see whether re-emitted
                            # chunks carry identical args or varied ones).
                            # Emit stays DEBUG to keep prod log volume sane;
                            # SKIP stays INFO — each skip is an actual signal.
                            import hashlib

                            _args_hash = hashlib.md5(
                                fc_args_json.encode("utf-8")
                            ).hexdigest()[:10]
                            if dedup_key in emitted_function_calls:
                                # Provider re-emitted the same functionCall in
                                # a later SSE chunk. Skip — downstream already
                                # accumulated it under a fresh uuid, and
                                # adding another copy would create a duplicate
                                # Activity-drawer pill.
                                logger.info(
                                    f"[GEMINI] functionCall SKIP (dup): "
                                    f"name={fc_name} args_hash={_args_hash}"
                                )
                                continue
                            emitted_function_calls.add(dedup_key)
                            logger.debug(
                                f"[GEMINI] functionCall emit: "
                                f"name={fc_name} args_hash={_args_hash}"
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
                                logger.debug(
                                    f"[GEMINI3] Captured thoughtSignature for {fc_name}"
                                )
                            tool_calls_batch.append(tool_call)

                        # Capture standalone thoughtSignature if present (rare but possible)
                        elif "thoughtSignature" in part and "functionCall" not in part:
                            logger.debug("[GEMINI3] Captured standalone thoughtSignature")
                            ts = part["thoughtSignature"]
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
                usage_meta = evt.get("usageMetadata", {})
                if usage_meta:
                    usage = {}
                    if "promptTokenCount" in usage_meta:
                        usage["input_tokens"] = usage_meta["promptTokenCount"]
                    if "candidatesTokenCount" in usage_meta:
                        usage["output_tokens"] = usage_meta["candidatesTokenCount"]
                    if usage:
                        yield StreamDelta(usage=usage)

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
