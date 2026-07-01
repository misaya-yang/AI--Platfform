"""
Query Intent Analyzer - LLM-Driven Dynamic Retrieval Decision.

This module implements Self-RAG style adaptive retrieval:
- Dynamically decides whether knowledge base search is needed
- Uses fast rules for obvious cases, LLM for complex decisions
- Provides observability through decision logging

Design Philosophy (Manus-inspired):
1. Don't waste retrieval on queries the model can answer directly
2. Ensure enterprise-specific content always gets KB context
3. Balance latency vs accuracy with tiered approach
4. Log decisions for observability and debugging

References:
- Self-RAG: https://selfrag.github.io/
- Manus Context Engineering: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger

if TYPE_CHECKING:
    pass


# Type alias for cache entries: (QueryIntent, timestamp)
CacheEntry = tuple["QueryIntent", float]

logger = get_logger(__name__)

# L02: Precompiled regex patterns (avoid re-compiling on every call)
_SLASH_COMMAND_RE = re.compile(r"^/([a-z][a-z0-9\-_]{1,48})\b")
_USE_SKILL_RE = re.compile(
    r"(?:use|用|调用|启用)\s*(?:skill|技能)\s*[:\s]*([a-z][a-z0-9\-_]+)"
)


class QueryType(str, Enum):
    """Query type classification."""

    FACTUAL = "factual"  # Questions with definitive answers
    CONVERSATIONAL = "conversational"  # Greetings, small talk
    CREATIVE = "creative"  # Creative writing, brainstorming
    ANALYTICAL = "analytical"  # Analysis, comparison, evaluation


class RetrievalDecision(str, Enum):
    """Retrieval decision types."""

    SKIP = "skip"  # Don't retrieve - model can answer
    RETRIEVE = "retrieve"  # Retrieve from KB
    UNCERTAIN = "uncertain"  # Not sure - default to retrieve


@dataclass
class QueryIntent:
    """
    LLM-analyzed query intent result.

    Provides rich context for intelligent routing decisions.
    """

    # Core decision
    requires_kb_search: bool
    decision: RetrievalDecision
    decision_reason: str

    # Query characteristics
    query_type: QueryType
    domain: str  # general_ai, company_specific, personal, etc.
    complexity: float  # 0-1 complexity score

    # Retrieval strategy (if needed)
    retrieval_strategy: str  # semantic, keyword, hybrid, none
    suggested_queries: list[str] = field(default_factory=list)

    # Skill detection
    skill_name: str | None = None  # Detected /slash-command or "use skill X"

    # Metadata
    confidence: float = 0.0
    analysis_time_ms: float = 0.0
    tier_used: str = "unknown"  # fast_rule, llm, default

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "requires_kb_search": self.requires_kb_search,
            "decision": self.decision.value,
            "decision_reason": self.decision_reason,
            "query_type": self.query_type.value,
            "domain": self.domain,
            "complexity": self.complexity,
            "retrieval_strategy": self.retrieval_strategy,
            "suggested_queries": self.suggested_queries,
            "confidence": self.confidence,
            "analysis_time_ms": self.analysis_time_ms,
            "tier_used": self.tier_used,
        }


class QueryIntentAnalyzer:
    """
    LLM-driven query intent analyzer.

    Implements tiered decision making:
    - Tier 1: Fast rules (0ms) - obvious cases
    - Tier 2: LLM analysis (~100ms) - complex cases
    - Tier 3: Default (0ms) - conservative fallback

    Usage:
        ```python
        analyzer = QueryIntentAnalyzer(model_registry)

        intent = await analyzer.analyze(
            query="什么是 flow matching?",
            available_datasets=["tech_docs", "product_info"],
        )

        if intent.requires_kb_search:
            # Perform retrieval
            results = await kb_service.search(intent.suggested_queries)
        else:
            # Skip retrieval, use model knowledge
            pass
        ```
    """

    def __init__(
        self,
        model_registry: Any = None,
        model_name: str = "qwen3.7-plus",
        enable_llm_tier: bool = True,
        cache_ttl: int = 3600,
        cache_max_size: int = 1000,
    ):
        """
        Initialize the analyzer.

        Args:
            model_registry: Model registry for LLM calls
            model_name: Model to use for analysis (should be fast model)
            enable_llm_tier: Whether to enable LLM-based analysis
            cache_ttl: Cache TTL in seconds
            cache_max_size: Maximum cache entries before eviction
        """
        self.model_registry = model_registry
        self.model_name = model_name
        self.enable_llm_tier = enable_llm_tier
        self.cache_ttl = cache_ttl
        self.cache_max_size = cache_max_size
        # Instance-level cache with TTL support: {cache_key: (QueryIntent, timestamp)}
        self._cache: dict[str, CacheEntry] = {}

    async def analyze(
        self,
        query: str,
        available_datasets: list[str] | None = None,
        user_context: dict[str, Any] | None = None,
    ) -> QueryIntent:
        """
        Analyze query intent and decide on retrieval.

        Uses tiered approach:
        1. Fast rules for obvious cases (0ms)
        2. LLM analysis for complex cases (~100ms)
        3. Conservative default

        Args:
            query: User's query
            available_datasets: Available KB datasets
            user_context: Additional user context

        Returns:
            QueryIntent with decision and reasoning
        """
        start_time = time.time()

        # Tier 1: Fast rules
        fast_result = self._fast_check(query)
        if fast_result is not None:
            fast_result.analysis_time_ms = (time.time() - start_time) * 1000
            logger.debug(
                f"[INTENT] Tier 1 decision: {fast_result.decision.value}, "
                f"reason={fast_result.decision_reason}"
            )
            return fast_result

        # Tier 2: LLM analysis (if enabled and model available)
        if self.enable_llm_tier and self.model_registry:
            llm_result = await self._llm_analyze(query, available_datasets or [], user_context)
            if llm_result is not None:
                llm_result.analysis_time_ms = (time.time() - start_time) * 1000
                logger.debug(
                    f"[INTENT] Tier 2 decision: {llm_result.decision.value}, "
                    f"reason={llm_result.decision_reason}"
                )
                return llm_result

        # Tier 3: Default - conservative
        default_result = self._default_decision(query)
        default_result.analysis_time_ms = (time.time() - start_time) * 1000
        logger.debug(f"[INTENT] Tier 3 default: {default_result.decision.value}")
        return default_result

    def _fast_check(self, query: str) -> QueryIntent | None:
        """
        Tier 1: Fast rule-based check.

        Handles obvious cases without LLM call.
        Cost: ~0ms
        """
        query_lower = query.lower().strip()
        query_len = len(query)

        # 0. Skill slash-command detection: /skill-name or "use skill X"
        slash_match = _SLASH_COMMAND_RE.match(query_lower)
        if slash_match:
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason=f"Slash command: /{slash_match.group(1)}",
                query_type=QueryType.FACTUAL,
                domain="skill",
                complexity=0.2,
                retrieval_strategy="none",
                confidence=0.95,
                tier_used="fast_rule",
                skill_name=slash_match.group(1),
            )
        use_skill_match = _USE_SKILL_RE.search(query_lower)
        if use_skill_match:
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason=f"Explicit skill invocation: {use_skill_match.group(1)}",
                query_type=QueryType.FACTUAL,
                domain="skill",
                complexity=0.2,
                retrieval_strategy="none",
                confidence=0.9,
                tier_used="fast_rule",
                skill_name=use_skill_match.group(1),
            )

        # 1. Greetings - no KB needed
        greetings = ["你好", "早上好", "晚上好", "嗨", "hi", "hello", "hey", "哈喽"]
        if any(query_lower.startswith(g) for g in greetings) and query_len < 20:
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="Greeting detected",
                query_type=QueryType.CONVERSATIONAL,
                domain="general",
                complexity=0.1,
                retrieval_strategy="none",
                confidence=0.95,
                tier_used="fast_rule",
            )

        # 1.5 Simple/short queries - no KB needed
        # Queries under 10 chars are almost always greetings, thanks, or simple commands
        if query_len <= 10 and not any(k in query_lower for k in ["搜索", "查询", "查找", "查一下", "找"]):
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="Short simple query",
                query_type=QueryType.CONVERSATIONAL,
                domain="general",
                complexity=0.1,
                retrieval_strategy="none",
                confidence=0.85,
                tier_used="fast_rule",
            )

        # 1.6 Common conversational patterns - no KB
        # Guard: skip this rule if enterprise keywords are present (need KB)
        enterprise_guard = ["公司", "产品", "客户", "流程", "政策", "内部", "审批", "员工", "部门", "报表"]
        has_enterprise_kw = any(kw in query_lower for kw in enterprise_guard)

        conversational_patterns = [
            "谢谢", "感谢", "好的", "明白", "了解", "知道了", "收到",
            "没问题", "可以", "不用了", "算了", "再见", "拜拜",
            "thank", "thanks", "ok", "okay", "got it", "sure", "yes", "no",
            "帮我写", "帮我翻译", "翻译一下", "总结一下", "帮我改",
            "写一个", "写一段", "生成一个", "创建一个",
        ]
        if not has_enterprise_kw and any(query_lower.startswith(p) or query_lower == p for p in conversational_patterns):
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="Conversational/creative pattern",
                query_type=QueryType.CONVERSATIONAL,
                domain="general",
                complexity=0.2,
                retrieval_strategy="none",
                confidence=0.8,
                tier_used="fast_rule",
            )

        # 1.7 Code-related queries - rarely need KB (also guarded by enterprise keywords)
        code_patterns = ["代码", "bug", "报错", "error", "debug", "修复", "重构", "refactor", "实现"]
        if not has_enterprise_kw and any(p in query_lower for p in code_patterns) and not any(k in query_lower for k in ["文档", "知识库"]):
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="Code/technical task - use model knowledge",
                query_type=QueryType.ANALYTICAL,
                domain="code",
                complexity=0.5,
                retrieval_strategy="none",
                confidence=0.75,
                tier_used="fast_rule",
            )

        # 2. System capability questions - no KB needed
        capability_patterns = [
            "你能做什么",
            "你的功能",
            "你支持什么",
            "你会什么",
            "你是谁",
            "你叫什么",
            "你的名字",
            "介绍一下你自己",
            "怎么使用你",
            "如何使用你",
        ]
        if any(p in query_lower for p in capability_patterns):
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="System capability query",
                query_type=QueryType.CONVERSATIONAL,
                domain="system",
                complexity=0.2,
                retrieval_strategy="none",
                confidence=0.9,
                tier_used="fast_rule",
            )

        # 3. General AI/ML/Math knowledge - no KB needed
        # NOTE: This is NOT meant to be exhaustive. The LLM tier will catch
        # anything these fast rules miss. These are just common cases to
        # reduce LLM calls and improve latency.
        general_ai_keywords = [
            # Core ML/DL concepts
            "机器学习",
            "深度学习",
            "神经网络",
            "transformer",
            "attention",
            "bert",
            "gpt",
            "llm",
            "大模型",
            "大语言模型",
            # Generative models
            "flow matching",
            "diffusion",
            "扩散模型",
            "生成模型",
            "gan",
            "vae",
            "sde",
            "ode",
            "score function",
            "denoising",
            "noise schedule",
            # Embeddings and retrieval
            "embedding",
            "向量",
            "词向量",
            "rag",
            "检索增强",
            # Agents
            "agent",
            "智能体",
            "multi-agent",
            # Math/Stats concepts common in ML
            "概率",
            "分布",
            "采样",
            "梯度",
            "损失函数",
            "优化",
            "马尔可夫",
            "蒙特卡洛",
            "变分",
            "贝叶斯",
            # Common architectures
            "cnn",
            "rnn",
            "lstm",
            "resnet",
            "unet",
        ]

        explanation_patterns = [
            "什么是",
            "解释一下",
            "介绍一下",
            "讲解一下",
            "原理",
            "为什么",
            "如何理解",
        ]
        is_explanation = any(p in query_lower for p in explanation_patterns)
        has_ai_keyword = any(kw in query_lower for kw in general_ai_keywords)

        if is_explanation and has_ai_keyword:
            return QueryIntent(
                requires_kb_search=False,
                decision=RetrievalDecision.SKIP,
                decision_reason="General AI/ML knowledge query",
                query_type=QueryType.FACTUAL,
                domain="general_ai",
                complexity=0.4,
                retrieval_strategy="none",
                confidence=0.85,
                tier_used="fast_rule",
            )

        # 4. Enterprise-specific - definitely need KB
        enterprise_keywords = [
            "我们公司",
            "公司的",
            "内部",
            "产品",
            "服务",
            "政策",
            "流程",
            "文档",
            "客户",
            "订单",
        ]
        if any(kw in query_lower for kw in enterprise_keywords):
            return QueryIntent(
                requires_kb_search=True,
                decision=RetrievalDecision.RETRIEVE,
                decision_reason="Enterprise-specific content",
                query_type=QueryType.FACTUAL,
                domain="company_specific",
                complexity=0.5,
                retrieval_strategy="hybrid",
                suggested_queries=[query],
                confidence=0.9,
                tier_used="fast_rule",
            )

        # No clear fast decision
        return None

    async def _llm_analyze(
        self,
        query: str,
        available_datasets: list[str],
        user_context: dict[str, Any] | None,
    ) -> QueryIntent | None:
        """
        Tier 2: LLM-based analysis.

        Uses fast model for quick decision.
        Cost: ~50-100ms
        """
        _ = user_context
        # Check cache with TTL
        cache_key = self._get_cache_key(query, available_datasets)
        cached_result = self._get_from_cache(cache_key)
        if cached_result is not None:
            # Return a copy to avoid mutating cached object
            cached_result.tier_used = "cache"
            return cached_result

        try:
            prompt = self._build_analysis_prompt(query, available_datasets)

            response = await self.model_registry.chat(
                model_id=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            )

            # Parse response
            content = response.content if hasattr(response, "content") else str(response)
            result = self._parse_llm_response(content, query)

            # Cache result with TTL
            self._set_cache(cache_key, result)

            return result

        except Exception as e:
            logger.warning(f"LLM intent analysis failed: {e}")
            return None

    def _get_from_cache(self, cache_key: str) -> QueryIntent | None:
        """
        Get from cache with TTL check.

        Returns None if not found or expired.
        """
        if cache_key not in self._cache:
            return None

        intent, timestamp = self._cache[cache_key]
        current_time = time.time()

        # Check if expired
        if current_time - timestamp > self.cache_ttl:
            # Remove expired entry
            del self._cache[cache_key]
            logger.debug(f"[INTENT CACHE] Expired entry removed: {cache_key[:8]}...")
            return None

        return intent

    def _set_cache(self, cache_key: str, intent: QueryIntent) -> None:
        """
        Set cache entry with timestamp.

        Handles eviction when cache is full.
        """
        current_time = time.time()

        # Evict expired entries first if cache is near capacity
        if len(self._cache) >= self.cache_max_size:
            self._evict_expired_entries(current_time)

        # If still at capacity, evict oldest entries (LRU-like)
        if len(self._cache) >= self.cache_max_size:
            self._evict_oldest_entries(evict_count=self.cache_max_size // 10 or 1)

        # Store with timestamp
        self._cache[cache_key] = (intent, current_time)

    def _evict_expired_entries(self, current_time: float) -> int:
        """
        Remove all expired cache entries.

        Returns number of entries evicted.
        """
        expired_keys = [
            key
            for key, (_, timestamp) in self._cache.items()
            if current_time - timestamp > self.cache_ttl
        ]

        for key in expired_keys:
            del self._cache[key]

        if expired_keys:
            logger.debug(f"[INTENT CACHE] Evicted {len(expired_keys)} expired entries")

        return len(expired_keys)

    def _evict_oldest_entries(self, evict_count: int) -> None:
        """
        Evict oldest entries when cache is full.

        Uses timestamp for LRU-like eviction.
        """
        if not self._cache:
            return

        # Sort by timestamp and remove oldest
        sorted_entries = sorted(
            self._cache.items(),
            key=lambda x: x[1][1],  # Sort by timestamp
        )

        for key, _ in sorted_entries[:evict_count]:
            del self._cache[key]

        logger.debug(f"[INTENT CACHE] Evicted {evict_count} oldest entries")

    def _build_analysis_prompt(
        self,
        query: str,
        available_datasets: list[str],
    ) -> str:
        """Build prompt for LLM analysis."""
        datasets_str = ", ".join(available_datasets) if available_datasets else "无"

        return f"""判断用户查询是否需要从知识库检索。直接返回 JSON，不要其他内容。

用户查询: "{query}"
可用知识库: {datasets_str}

决策规则:
1. 通用 AI/技术知识 → requires_kb_search: false
2. 企业特定内容（产品、政策、流程）→ requires_kb_search: true
3. 不确定 → requires_kb_search: true

JSON 格式:
{{"requires_kb_search": true/false, "reason": "简短理由", "domain": "general_ai/company_specific/other", "complexity": 0.0-1.0}}"""

    def _parse_llm_response(self, response: str, query: str) -> QueryIntent:
        """Parse LLM response into QueryIntent."""
        try:
            # Try to extract JSON with nested object support
            json_str = self._extract_json(response)
            if json_str:
                data = json.loads(json_str)

                requires_kb = data.get("requires_kb_search", True)
                return QueryIntent(
                    requires_kb_search=requires_kb,
                    decision=RetrievalDecision.RETRIEVE if requires_kb else RetrievalDecision.SKIP,
                    decision_reason=data.get("reason", "LLM decision"),
                    query_type=QueryType.FACTUAL,
                    domain=data.get("domain", "unknown"),
                    complexity=data.get("complexity", 0.5),
                    retrieval_strategy="hybrid" if requires_kb else "none",
                    suggested_queries=[query] if requires_kb else [],
                    confidence=0.85,
                    tier_used="llm",
                )

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Failed to parse LLM response: {e}")

        # Default on parse failure
        return self._default_decision(query)

    def _extract_json(self, text: str) -> str | None:
        """
        Extract JSON object from text, handling nested braces.

        Uses brace counting to find matching pairs.
        """
        # Find the first '{'
        start_idx = text.find("{")
        if start_idx == -1:
            return None

        # Count braces to find matching '}'
        brace_count = 0
        in_string = False
        escape_next = False

        for i, char in enumerate(text[start_idx:], start=start_idx):
            if escape_next:
                escape_next = False
                continue

            if char == "\\" and in_string:
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    return text[start_idx : i + 1]

        # No matching closing brace found
        return None

    def _default_decision(self, query: str) -> QueryIntent:
        """
        Tier 3: Default conservative decision.

        When in doubt, retrieve.
        """
        return QueryIntent(
            requires_kb_search=True,
            decision=RetrievalDecision.RETRIEVE,
            decision_reason="Default conservative decision",
            query_type=QueryType.FACTUAL,
            domain="unknown",
            complexity=0.5,
            retrieval_strategy="hybrid",
            suggested_queries=[query],
            confidence=0.5,
            tier_used="default",
        )

    def _get_cache_key(self, query: str, datasets: list[str]) -> str:
        """Generate cache key."""
        content = f"{query}|{','.join(sorted(datasets))}"
        return hashlib.md5(content.encode()).hexdigest()

    def clear_cache(self) -> int:
        """
        Clear all cache entries.

        Returns number of entries cleared.
        """
        count = len(self._cache)
        self._cache.clear()
        logger.info(f"[INTENT CACHE] Cleared {count} entries")
        return count

    @property
    def cache_stats(self) -> dict[str, Any]:
        """
        Get cache statistics for observability.

        Returns dict with cache metrics.
        """
        current_time = time.time()
        expired_count = sum(
            1
            for _, (_, timestamp) in self._cache.items()
            if current_time - timestamp > self.cache_ttl
        )

        return {
            "size": len(self._cache),
            "max_size": self.cache_max_size,
            "ttl_seconds": self.cache_ttl,
            "expired_count": expired_count,
            "utilization": len(self._cache) / self.cache_max_size if self.cache_max_size > 0 else 0,
        }


# =============================================================================
# Factory Function
# =============================================================================


def create_query_intent_analyzer(
    model_registry: Any = None,
    model_name: str = "qwen3.7-plus",
    enable_llm_tier: bool = True,
    cache_ttl: int = 3600,
    cache_max_size: int = 1000,
) -> QueryIntentAnalyzer:
    """Create a QueryIntentAnalyzer instance."""
    return QueryIntentAnalyzer(
        model_registry=model_registry,
        model_name=model_name,
        enable_llm_tier=enable_llm_tier,
        cache_ttl=cache_ttl,
        cache_max_size=cache_max_size,
    )
