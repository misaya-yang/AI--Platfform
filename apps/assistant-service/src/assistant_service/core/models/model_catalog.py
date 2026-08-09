"""Model metadata, capabilities, and the built-in provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_gateway_core.enums import ModelAccessLevel, ModelProvider

from .responses_api import CHAT_COMPLETIONS_WIRE_PROTOCOL

_QWEN_THINKING_DISABLED_LEVELS = frozenset({"disabled", "false", "none", "off"})


def _qwen_thinking_enabled(model_id: str, thinking_level: str | None) -> bool | None:
    """Return an explicit Qwen thinking flag without changing provider defaults."""
    if thinking_level is None or "qwen3" not in model_id.lower():
        return None
    return thinking_level.strip().lower() not in _QWEN_THINKING_DISABLED_LEVELS


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
    #: Versioned upstream wire protocol. Responses v1 is opt-in per provider;
    #: OpenAI-compatible chat completions remains the compatibility default.
    wire_protocol: str = CHAT_COMPLETIONS_WIRE_PROTOCOL


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
