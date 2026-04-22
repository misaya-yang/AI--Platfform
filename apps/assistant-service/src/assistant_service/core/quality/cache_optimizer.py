"""
KV-Cache Optimization for Assistant Service

Implements Manus-style context caching with three cache layers:
- Layer 1: Static prefix (system prompt + tools) - cross-session reuse
- Layer 2: Session context (KB + history) - intra-session reuse
- Layer 3: Current input (dynamic)
"""

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any


class CacheBreakpoint(Enum):
    """Cache control markers for provider APIs."""

    EPHEMERAL = "ephemeral"  # Short-lived cache (5-60 min)


@dataclass
class CacheConfig:
    """Configuration for KV-cache optimization."""

    enable_layer1_cache: bool = True  # System prefix caching
    enable_layer2_cache: bool = True  # Session context caching
    layer1_ttl_minutes: int = 60  # System prefix TTL
    layer2_ttl_minutes: int = 10  # Session context TTL
    min_cacheable_tokens: int = 1024  # Minimum tokens for caching


@dataclass
class CacheMetrics:
    """Metrics for cache performance tracking."""

    layer1_hit: bool = False
    layer2_hit: bool = False
    cached_tokens: int = 0
    total_input_tokens: int = 0
    cache_hit_rate: float = 0.0
    estimated_savings_usd: float = 0.0
    system_prefix_hash: str = ""


class ContextCacheOptimizer:
    """
    Optimizes context for maximum KV-cache hit rate.

    Key strategies:
    1. Deterministic system prompt (no timestamps, sorted tools)
    2. Stable context ordering (system -> KB -> history -> user)
    3. Explicit cache breakpoints for provider APIs
    """

    def __init__(self, config: CacheConfig = None):
        self.config = config or CacheConfig()
        self._system_prefix_hash: str | None = None

    def build_optimized_messages(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]] | None,
        kb_context: str | None,
        web_context: str | None,
        history: list[dict[str, Any]],
        current_message: str,
        provider: str,  # "gemini" | "dashscope"
    ) -> list[dict[str, Any]]:
        """Build messages with optimal cache structure.

        Args:
            system_prompt: Base system prompt.
            tools: Optional list of tool definitions.
            kb_context: Optional knowledge base context.
            web_context: Optional web search context.
            history: Conversation history.
            current_message: Current user message.
            provider: LLM provider identifier. Reserved for future
                provider-specific message formatting.

        Returns:
            List of messages optimized for cache hit rate.
        """
        messages = []

        # === Layer 1: Static Prefix ===
        system_content = self._build_deterministic_system(system_prompt, tools)
        system_msg = {"role": "system", "content": system_content}

        if self.config.enable_layer1_cache:
            system_msg["cache_control"] = {"type": CacheBreakpoint.EPHEMERAL.value}

        messages.append(system_msg)

        # === Layer 2: Session Context ===
        if kb_context:
            messages.append({"role": "user", "content": f"[参考资料]\n{kb_context}"})
            messages.append(
                {"role": "assistant", "content": "我已阅读参考资料，将基于这些信息回答问题。"}
            )

        if web_context:
            messages.append({"role": "user", "content": f"[网络搜索结果]\n{web_context}"})
            messages.append({"role": "assistant", "content": "我已获取最新的网络搜索结果。"})

        for msg in history:
            messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", "")
                    if isinstance(msg.get("content"), str)
                    else str(msg.get("content", "")),
                }
            )

        if self.config.enable_layer2_cache and len(messages) > 1:
            messages[-1]["cache_control"] = {"type": CacheBreakpoint.EPHEMERAL.value}

        # === Layer 3: Current Input ===
        messages.append({"role": "user", "content": current_message})

        return messages

    def _build_deterministic_system(
        self, base_prompt: str, tools: list[dict[str, Any]] | None = None
    ) -> str:
        """Build byte-identical system prompt for cache stability."""
        parts = [base_prompt.strip()]

        if tools:
            sorted_tools = sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))
            tools_section = "\n\n## 可用工具\n"
            for tool in sorted_tools:
                func = tool.get("function", {})
                name = func.get("name", "unknown")
                desc = func.get("description", "")
                tools_section += f"\n### {name}\n{desc}\n"
            parts.append(tools_section)

        system_content = "\n".join(parts)
        self._system_prefix_hash = hashlib.md5(system_content.encode()).hexdigest()[:16]

        return system_content

    def calculate_cache_savings(
        self, total_tokens: int, cached_tokens: int, provider: str
    ) -> float:
        """Calculate estimated cost savings from cache hits."""
        pricing = {
            "gemini": {"cached": 0.075, "uncached": 0.75},
            "dashscope": {"cached": 0.0005, "uncached": 0.002},
        }

        rates = pricing.get(provider, pricing["dashscope"])
        full_cost = (total_tokens / 1_000_000) * rates["uncached"]
        actual_cost = (cached_tokens / 1_000_000) * rates["cached"] + (
            (total_tokens - cached_tokens) / 1_000_000
        ) * rates["uncached"]

        return full_cost - actual_cost

    def parse_cache_metrics(self, response_usage: dict[str, Any], provider: str) -> CacheMetrics:
        """Extract cache metrics from LLM API response."""
        metrics = CacheMetrics()

        if provider == "gemini":
            metrics.total_input_tokens = response_usage.get("promptTokenCount", 0)
            metrics.cached_tokens = response_usage.get("cachedContentTokenCount", 0)
        elif provider == "dashscope":
            usage = response_usage.get("prompt_tokens_details", {})
            metrics.total_input_tokens = response_usage.get("input_tokens", 0)
            metrics.cached_tokens = usage.get("cached_tokens", 0)

        if metrics.total_input_tokens > 0:
            metrics.cache_hit_rate = metrics.cached_tokens / metrics.total_input_tokens

        metrics.estimated_savings_usd = self.calculate_cache_savings(
            metrics.total_input_tokens, metrics.cached_tokens, provider
        )
        metrics.system_prefix_hash = self._system_prefix_hash or ""

        return metrics
