"""
Tool Selector — token-aware, relevance-scored tool selection.

ADR-003 Phase 2: Replaces the binary question/creation filter with
a scoring system that:
1. Scores each tool by relevance to the user message (0-1.0)
2. Prioritizes builtin tools over MCP/skill tools
3. Enforces a token budget to prevent context explosion
4. Always includes universal tools (KB search, memory)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ....core.observability.logging import get_logger
from .constants import ToolName

if TYPE_CHECKING:
    from .tool_registry import ToolDefinition

logger = get_logger(__name__)

# Default budget: ~2000 tokens for tool schemas (compact mode)
DEFAULT_TOOL_TOKEN_BUDGET = 2000

# Priority tiers (lower = higher priority, included first when budget tight)
TIER_ALWAYS = 0   # Always included (KB search, memory)
TIER_BUILTIN = 1  # Builtin generation/analysis tools
TIER_SKILL = 2    # User-defined skills
TIER_MCP = 3      # MCP tools (lowest priority by default)

# Tools that are always included regardless of relevance
ALWAYS_INCLUDE = {ToolName.SEARCH_KB, ToolName.UPDATE_MEMORY}

# Keyword → tool relevance mappings
_TOOL_KEYWORDS: dict[str, list[str]] = {
    ToolName.SEARCH_KB: [
        "knowledge base", "knowledge", "kb", "internal document", "查找", "知识库", "知识", "文档",
    ],
    ToolName.SEARCH_WEB: [
        "web", "internet", "news", "current", "latest", "online", "网上", "新闻", "最新",
        "search online", "google",
    ],
    ToolName.EXECUTE_CODE: [
        "code", "python", "run", "execute", "script", "calculate", "compute", "plot",
        "chart", "data", "analyze", "代码", "运行", "计算", "分析", "图表", "脚本",
    ],
    ToolName.GENERATE_IMAGE: [
        "image", "picture", "photo", "draw", "illustration", "poster", "图片", "图像",
        "画", "海报", "生成图",
    ],
    ToolName.GENERATE_DOCUMENT: [
        "document", "docx", "word", "pdf", "report", "paper", "write", "draft",
        "文档", "报告", "论文", "word",
    ],
    ToolName.GENERATE_PPTX: [
        "ppt", "pptx", "slide", "presentation", "powerpoint", "幻灯片", "演示",
    ],
    ToolName.GENERATE_QUIZ: [
        "quiz", "test", "exam", "question", "practice", "测验", "考试", "题",
        "出题", "考考",
    ],
    ToolName.UPDATE_MEMORY: [
        "remember", "memory", "preference", "记住", "记忆", "偏好",
    ],
}

# MCP server name → keywords that trigger inclusion
_MCP_SERVER_KEYWORDS: dict[str, list[str]] = {
    "wahda": ["wahda", "post", "message", "social", "社交", "帖子", "消息"],
    "halalmoney": ["halal", "haram", "investment", "portfolio", "stock", "finance",
                   "清真", "投资", "股票", "金融", "bitcoin", "btc", "aapl"],
}


def _estimate_tool_tokens(tool_def: ToolDefinition, compact: bool = True) -> int:
    """Estimate token count for a tool's schema."""
    try:
        schema = tool_def.to_openai_schema(compact=compact)
        text = json.dumps(schema, ensure_ascii=False, default=str)
        # Rough estimate: ~4 chars per token for JSON
        return len(text) // 4
    except Exception:
        return 80  # Conservative fallback


def _score_tool(tool_def: ToolDefinition, message_lower: str) -> float:
    """Score a tool's relevance to the user message (0.0 - 1.0)."""
    name = tool_def.name

    # Always-include tools get max score
    if name in ALWAYS_INCLUDE:
        return 1.0

    # Check builtin keyword matches
    keywords = _TOOL_KEYWORDS.get(name, [])
    if keywords:
        hits = sum(1 for kw in keywords if kw in message_lower)
        if hits:
            return min(0.3 + hits * 0.2, 1.0)  # 0.5 for 1 hit, 0.7 for 2, etc.
        return 0.1  # Builtin but no keyword match — low baseline

    # MCP tools: check server-specific keywords
    if name.startswith("mcp_"):
        for server, kws in _MCP_SERVER_KEYWORDS.items():
            if name.startswith(f"mcp_{server}__"):
                hits = sum(1 for kw in kws if kw in message_lower)
                if hits:
                    return min(0.4 + hits * 0.2, 1.0)
                return 0.0  # MCP tool but no relevance signal — exclude
        # Fallback for unknown MCP servers: match server name or tool action against message
        parts = name.split("__", 1)
        server_part = parts[0].replace("mcp_", "") if parts else ""
        action_part = parts[1] if len(parts) > 1 else ""
        if server_part and server_part in message_lower:
            return 0.6
        if action_part and action_part.replace("_", " ") in message_lower:
            return 0.5
        return 0.0

    # Skill tools: moderate baseline
    if tool_def.category and tool_def.category.value == "skill":
        return 0.2

    return 0.1  # Unknown tool — low priority


def _get_tier(tool_def: ToolDefinition) -> int:
    """Get priority tier for a tool."""
    if tool_def.name in ALWAYS_INCLUDE:
        return TIER_ALWAYS
    if tool_def.name.startswith("mcp_"):
        return TIER_MCP
    cat = tool_def.category.value if tool_def.category else ""
    if cat == "skill":
        return TIER_SKILL
    return TIER_BUILTIN


def select_tools(
    all_tools: list[ToolDefinition],
    user_message: str,
    max_tokens: int = DEFAULT_TOOL_TOKEN_BUDGET,
) -> list[ToolDefinition]:
    """
    Select tools for a request with relevance scoring and token budget.

    Strategy:
    1. Score each tool by relevance to user_message
    2. Sort by (tier ASC, score DESC) — always-include first, then highest scoring
    3. Include tools until token budget exhausted
    4. Always include TIER_ALWAYS tools regardless of budget

    Returns:
        List of selected ToolDefinitions, ordered by tier then score.
    """
    if not all_tools:
        return []

    message_lower = (user_message or "").lower()

    # Score and annotate each tool
    scored: list[tuple[ToolDefinition, float, int, int]] = []
    for tool in all_tools:
        score = _score_tool(tool, message_lower)
        tier = _get_tier(tool)
        tokens = _estimate_tool_tokens(tool)
        scored.append((tool, score, tier, tokens))

    # Sort: tier ASC, score DESC
    scored.sort(key=lambda x: (x[2], -x[1]))

    # Select within budget
    selected: list[ToolDefinition] = []
    used_tokens = 0

    for tool, score, tier, tokens in scored:
        if tier == TIER_ALWAYS:
            # Always include regardless of budget
            selected.append(tool)
            used_tokens += tokens
        elif score <= 0.0:
            # Zero relevance — skip entirely
            continue
        elif used_tokens + tokens <= max_tokens:
            selected.append(tool)
            used_tokens += tokens
        else:
            # Budget exhausted — log what was dropped
            logger.debug(
                f"Tool '{tool.name}' dropped (score={score:.2f}, tokens={tokens}, "
                f"budget={used_tokens}/{max_tokens})"
            )

    if len(selected) < len(all_tools):
        logger.info(
            f"[ToolSelector] {len(selected)}/{len(all_tools)} tools selected, "
            f"{used_tokens}/{max_tokens} tokens used"
        )

    return selected
