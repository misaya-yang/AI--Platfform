"""
Task Planning Prompts for Enterprise Agent.

These prompts guide the Agent in:
1. Understanding user intent
2. Planning task execution
3. Selecting appropriate strategies

Guardrails constrain tool usage, Agent decides execution approach.
"""

from typing import Any, Dict, List, Optional
import json


# =============================================================================
# Task Planning System Prompt
# =============================================================================

TASK_PLANNING_SYSTEM_PROMPT = """你是一个智能任务规划助手。

## 你的职责

分析用户请求，制定执行计划。

## 必须遵守的约束（Guardrails）

1. **工具使用约束**：
{tool_constraints}

2. **执行顺序约束**：
   - 搜索/检索必须在分析之前
   - 内容必须在文档生成之前
   - 依赖任务必须先完成

## 你的自由空间（Agent决策）

在满足约束的前提下，你可以决定：

- 任务如何分解（细粒度 vs 粗粒度）
- 哪些任务可以并行
- 使用什么工具组合
- 是否需要额外的验证步骤
- 错误恢复策略

## 输出格式

```json
{{
    "goal": "任务目标",
    "strategy": "执行策略说明",
    "tasks": [
        {{
            "id": "task_1",
            "type": "retrieve|generate|analyze|transform",
            "tool": "工具名称",
            "description": "任务描述",
            "parameters": {{}},
            "dependencies": []
        }}
    ]
}}
```"""


# =============================================================================
# Intent Analysis Prompt
# =============================================================================

INTENT_ANALYSIS_PROMPT = """分析以下用户请求的意图：

**用户请求**：{request}

判断：
1. 主要意图类型：document_creation / information_query / data_analysis / comparison / creative_writing / code_execution / general
2. 如果是文档创建，文档类型是什么（ppt/docx/xlsx/markdown）
3. 如果是对比，对比的对象是什么
4. 复杂度评估：simple / moderate / complex

输出JSON：
```json
{{
    "intent_type": "...",
    "document_type": "...",
    "comparison_items": [],
    "complexity": "...",
    "suggested_strategy": "simple|sequential|parallel|iterative"
}}
```"""


# =============================================================================
# Prompt Builder Functions
# =============================================================================

def build_planning_prompt(
    user_request: str,
    available_tools: List[Dict[str, Any]],
    tool_constraints: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """
    Build task planning prompt with guardrails.

    Args:
        user_request: User's request
        available_tools: List of available tools
        tool_constraints: Tool usage constraints (guardrails)

    Returns:
        Formatted planning prompt
    """
    # Format tool descriptions
    tool_descriptions = []
    for tool in available_tools:
        if isinstance(tool, str):
            tool_descriptions.append(f"- {tool}")
        elif isinstance(tool, dict):
            name = tool.get("function", {}).get("name", tool.get("name", "unknown"))
            desc = tool.get("function", {}).get("description", tool.get("description", ""))
            tool_descriptions.append(f"- {name}: {desc}")

    tool_info = "\n".join(tool_descriptions) if tool_descriptions else "无特定工具要求"

    # Format constraints
    if tool_constraints:
        constraint_lines = []
        for tool_name, constraints in tool_constraints.items():
            desc = constraints.get("description", "")
            constraint_lines.append(f"   - {tool_name}: {desc}")
        constraints_text = "\n".join(constraint_lines)
    else:
        constraints_text = "   无特殊约束"

    system_prompt = TASK_PLANNING_SYSTEM_PROMPT.format(
        tool_constraints=constraints_text,
    )

    user_prompt = f"""分析以下用户请求并制定执行计划：

**用户请求**：{user_request}

**可用工具**：
{tool_info}

请输出执行计划。"""

    return f"{system_prompt}\n\n---\n\n{user_prompt}"


def build_intent_analysis_prompt(request: str) -> str:
    """
    Build intent analysis prompt.

    Args:
        request: User's request

    Returns:
        Formatted intent analysis prompt
    """
    return INTENT_ANALYSIS_PROMPT.format(request=request)
