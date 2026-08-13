"""Deterministic, token-aware selection over a dynamic tool catalog.

Selection is a prompt-size optimization, never an authorization mechanism.
Unknown tools keep a non-zero baseline and the always-visible discovery
bridges provide access to the authorized catalog when a full schema is not
selected directly.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger, record_internal_exception

from .constants import ToolName
from .tool_discovery import DISCOVERY_TOOL_NAMES

if TYPE_CHECKING:
    from .tool_registry import ToolDefinition

logger = get_logger(__name__)

DEFAULT_TOOL_TOKEN_BUDGET = 2000

TIER_ALWAYS = 0
TIER_BUILTIN = 1
TIER_SKILL = 2
TIER_MCP = 3

ALWAYS_INCLUDE = {
    ToolName.SEARCH_KB,
    ToolName.UPDATE_MEMORY,
    ToolName.SPAWN_SUBAGENT,
    *DISCOVERY_TOOL_NAMES,
}

# Built-in aliases improve direct selection latency. Dynamic MCP and plugin
# tools do not need an entry here: their own catalog metadata is indexed.
_TOOL_KEYWORDS: dict[str, list[str]] = {
    ToolName.SEARCH_KB: [
        "knowledge base",
        "knowledge",
        "kb",
        "internal document",
        "查找",
        "知识库",
        "知识",
        "文档",
    ],
    ToolName.EXECUTE_CODE: [
        "code",
        "python",
        "run",
        "execute",
        "script",
        "calculate",
        "compute",
        "plot",
        "chart",
        "data",
        "analyze",
        "代码",
        "运行",
        "计算",
        "分析",
        "图表",
        "脚本",
    ],
    ToolName.GENERATE_IMAGE: [
        "image",
        "picture",
        "photo",
        "draw",
        "illustration",
        "poster",
        "图片",
        "图像",
        "画",
        "海报",
        "生成图",
    ],
    ToolName.GENERATE_DOCUMENT: [
        "document",
        "docx",
        "word",
        "pdf",
        "report",
        "paper",
        "write",
        "draft",
        "文档",
        "报告",
        "论文",
    ],
    ToolName.GENERATE_PPTX: [
        "ppt",
        "pptx",
        "slide",
        "presentation",
        "powerpoint",
        "幻灯片",
        "演示",
    ],
    ToolName.GENERATE_QUIZ: [
        "quiz",
        "test",
        "exam",
        "question",
        "practice",
        "测验",
        "考试",
        "题",
        "出题",
        "考考",
    ],
    ToolName.UPDATE_MEMORY: ["remember", "memory", "preference", "记住", "记忆", "偏好"],
}

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]", re.IGNORECASE)


def _estimate_tool_tokens(tool_def: ToolDefinition, compact: bool = True) -> int:
    try:
        schema = tool_def.to_openai_schema(compact=compact)
        return max(1, len(json.dumps(schema, ensure_ascii=False, default=str)) // 4)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tools.tool_selector.internal_failure", exc
        )
        return 80


def _metadata_values(tool_def: ToolDefinition) -> list[str]:
    metadata = getattr(tool_def, "capability_metadata", None) or {}
    values: list[Any] = [
        getattr(tool_def, "description", ""),
        getattr(tool_def, "when_to_use", "") or "",
        metadata.get("summary") or "",
        metadata.get("mcp_server") or metadata.get("server_id") or "",
        metadata.get("mcp_tool") or metadata.get("tool_id") or "",
    ]
    try:
        properties = tool_def.model_argument_schema().get("properties") or {}
        values.extend(str(name) for name in properties)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tools.tool_selector.internal_failure", exc
        )
        pass
    return [str(value).lower() for value in values if value]


def _score_tool(tool_def: ToolDefinition, message_lower: str) -> float:
    """Score catalog relevance without making unknown tools unreachable."""

    name = tool_def.name
    if name in ALWAYS_INCLUDE:
        return 1.0

    aliases = [
        *list(getattr(tool_def, "relevance_keywords", []) or []),
        *_TOOL_KEYWORDS.get(name, []),
    ]
    search_text = " ".join(
        [name.lower().replace("_", " ").replace("__", " "), *aliases, *_metadata_values(tool_def)]
    )
    phrase_hits = sum(1 for phrase in aliases if str(phrase).lower() in message_lower)
    query_tokens = set(_TOKEN_RE.findall(message_lower))
    catalog_tokens = set(_TOKEN_RE.findall(search_text))
    token_hits = len(query_tokens & catalog_tokens)
    if phrase_hits or token_hits:
        return min(0.4 + (phrase_hits * 0.2) + (token_hits * 0.08), 1.0)

    # Selection only controls which schemas are sent directly. A non-zero
    # baseline plus tool_search means an opaque newly-installed MCP is still
    # reachable without editing this module or guessing a server keyword.
    category = getattr(getattr(tool_def, "category", None), "value", "")
    return 0.15 if name.startswith("mcp_") or category in {"mcp", "skill"} else 0.1


def _get_tier(tool_def: ToolDefinition) -> int:
    if tool_def.name in ALWAYS_INCLUDE:
        return TIER_ALWAYS
    if tool_def.name.startswith("mcp_"):
        return TIER_MCP
    category = getattr(getattr(tool_def, "category", None), "value", "")
    if category == "skill":
        return TIER_SKILL
    return TIER_BUILTIN


def select_tools(
    all_tools: list[ToolDefinition],
    user_message: str,
    max_tokens: int = DEFAULT_TOOL_TOKEN_BUDGET,
) -> list[ToolDefinition]:
    """Select direct schemas while keeping discovery bridges visible."""

    if not all_tools:
        return []
    message_lower = (user_message or "").lower()
    scored: list[tuple[ToolDefinition, float, int, int]] = []
    for tool in all_tools:
        scored.append(
            (tool, _score_tool(tool, message_lower), _get_tier(tool), _estimate_tool_tokens(tool))
        )

    scored.sort(
        key=lambda item: (
            0 if item[2] == TIER_ALWAYS else 1,
            -item[1],
            item[2],
            item[0].name.casefold(),
        )
    )

    selected: list[ToolDefinition] = []
    used_tokens = 0
    for tool, score, tier, tokens in scored:
        if tier == TIER_ALWAYS or used_tokens + tokens <= max_tokens:
            selected.append(tool)
            used_tokens += tokens
        else:
            logger.debug(
                "Tool schema deferred (tool=%s score=%.2f tokens=%s budget=%s/%s)",
                tool.name,
                score,
                tokens,
                used_tokens,
                max_tokens,
            )

    if len(selected) < len(all_tools):
        logger.info(
            "[ToolSelector] %s/%s direct schemas, %s/%s tokens; deferred tools remain discoverable",
            len(selected),
            len(all_tools),
            used_tokens,
            max_tokens,
        )
    return selected
