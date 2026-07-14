"""
KV-Cache Optimization for Assistant Service

Implements Manus-style context caching with three cache layers:
- Layer 1: Static prefix (system prompt + tools) - cross-session reuse
- Layer 2: Session context (KB + history) - intra-session reuse
- Layer 3: Current input (dynamic)
"""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
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
        del provider  # Reserved for provider-specific message formatting.
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
        self._system_prefix_hash = stable_cache_hash(system_content)

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
        normalized = normalize_provider_cache_usage(response_usage, provider)

        metrics.total_input_tokens = normalized.get("input_tokens", 0)
        metrics.cached_tokens = normalized.get("cached_input_tokens", 0)
        metrics.cache_read_tokens = normalized.get("cache_read_input_tokens", 0)
        metrics.cache_creation_tokens = normalized.get("cache_creation_input_tokens", 0)

        if metrics.total_input_tokens > 0:
            metrics.cache_hit_rate = metrics.cached_tokens / metrics.total_input_tokens

        metrics.estimated_savings_usd = self.calculate_cache_savings(
            metrics.total_input_tokens, metrics.cached_tokens, provider
        )
        metrics.system_prefix_hash = self._system_prefix_hash or ""

        return metrics


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def stable_cache_hash(value: Any) -> str:
    """Return a stable bounded identity hash without exposing raw payloads."""
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def normalize_provider_cache_usage(
    response_usage: dict[str, Any] | None,
    provider: str | None = None,
) -> dict[str, int]:
    """Normalize provider usage fields into bounded integer telemetry.

    The returned shape is intentionally flat so it can pass through existing
    StreamDelta, AgentLoop usage aggregation, trace payloads, and JSONB storage.
    """
    del provider  # Provider is accepted for future provider-specific aliases.
    if not isinstance(response_usage, dict):
        return {}

    result: dict[str, int] = {}

    aliases = {
        "input_tokens": "input_tokens",
        "prompt_tokens": "input_tokens",
        "promptTokenCount": "input_tokens",
        "output_tokens": "output_tokens",
        "completion_tokens": "output_tokens",
        "candidatesTokenCount": "output_tokens",
        "total_tokens": "total_tokens",
        "totalTokenCount": "total_tokens",
        "cached_input_tokens": "cached_input_tokens",
        "cached_tokens": "cached_input_tokens",
        "cachedContentTokenCount": "cached_input_tokens",
        "cache_read_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens",
    }
    for source_key, target_key in aliases.items():
        parsed = _safe_int(response_usage.get(source_key))
        if parsed is not None:
            result[target_key] = max(result.get(target_key, 0), parsed)

    for detail_key in ("prompt_tokens_details", "input_tokens_details"):
        details = response_usage.get(detail_key)
        if not isinstance(details, dict):
            continue
        cached = _safe_int(details.get("cached_tokens"))
        if cached is not None:
            result["cached_input_tokens"] = max(result.get("cached_input_tokens", 0), cached)

    if "cached_input_tokens" not in result and result.get("cache_read_input_tokens"):
        result["cached_input_tokens"] = result["cache_read_input_tokens"]

    return result


def build_cache_usage_metrics(
    usage: dict[str, Any] | None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Build a trace-safe cache usage metrics payload."""
    normalized = normalize_provider_cache_usage(usage, provider)
    if not normalized:
        return {}
    total_input_tokens = normalized.get("input_tokens", 0)
    cached_tokens = normalized.get("cached_input_tokens", 0)
    payload: dict[str, Any] = {
        "provider": str(provider or "unknown"),
        "input_tokens": total_input_tokens,
        "cached_input_tokens": cached_tokens,
    }
    if "cache_read_input_tokens" in normalized:
        payload["cache_read_input_tokens"] = normalized["cache_read_input_tokens"]
    if "cache_creation_input_tokens" in normalized:
        payload["cache_creation_input_tokens"] = normalized["cache_creation_input_tokens"]
    if total_input_tokens > 0:
        payload["cache_hit_rate"] = round(cached_tokens / total_input_tokens, 6)
    return payload


def _tool_schema_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function.get("name") or tool.get("name") or "")


def _stable_tool_identity(tool: dict[str, Any]) -> dict[str, Any]:
    variable_fields = {"created_at", "updated_at", "last_used", "usage_count"}

    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): _clean(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key) not in variable_fields
            }
        if isinstance(value, list):
            return [_clean(item) for item in value]
        return value

    return _clean(tool)


@lru_cache(maxsize=1)
def runtime_source_hash() -> str:
    """Hash the loaded assistant Python source, including local hot updates."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_cache_context_metrics(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    provider: str | None = None,
    context_estimated_input_tokens: int | None = None,
    model_context_window: int | None = None,
) -> dict[str, Any]:
    """Build trace-safe prompt-prefix, tool-schema, cache, and context metrics."""
    tool_schemas = [tool for tool in (tools or []) if isinstance(tool, dict)]
    tool_names = [_tool_schema_name(tool) for tool in tool_schemas]
    stable_tool_schemas = [_stable_tool_identity(tool) for tool in tool_schemas]
    prefix_identity = {
        "system_prompt": system_prompt,
        "tools": stable_tool_schemas,
    }

    prefix_message_count = 0
    for message in messages:
        if message.get("role") == "system":
            prefix_message_count += 1
            continue
        break

    payload: dict[str, Any] = {
        "prompt_prefix_hash": stable_cache_hash(prefix_identity),
        "system_prompt_hash": stable_cache_hash(system_prompt),
        "prompt_prefix_message_count": prefix_message_count,
        "prompt_prefix_chars": len(system_prompt or ""),
        "tool_schema_count": len(tool_schemas),
        "tool_schema_hash": stable_cache_hash(stable_tool_schemas),
        "tool_schema_order_hash": stable_cache_hash(tool_names),
        "tool_schema_names_hash": stable_cache_hash(sorted(tool_names)),
        "runtime_revision": runtime_source_hash(),
    }

    if context_estimated_input_tokens is not None:
        payload["context_estimated_input_tokens"] = max(0, int(context_estimated_input_tokens))
    if model_context_window:
        window = max(0, int(model_context_window))
        payload["context_window_tokens"] = window
        estimated = int(context_estimated_input_tokens or 0)
        if window > 0:
            payload["context_utilization"] = round(min(estimated / window, 1.0), 6)

    cache_metrics = build_cache_usage_metrics(usage, provider)
    if cache_metrics:
        payload["provider_cache_metrics"] = cache_metrics

    return payload
