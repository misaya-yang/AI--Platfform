"""
Content Generation Prompts for Enterprise Agent.

These prompts clearly separate:
- Guardrails (必须遵守的底线)
- Agent Freedom (你的自由空间)

Agent operates freely within guardrail boundaries.
"""

from typing import Any, Dict, List, Optional

from ..guardrails import DocumentType, QUALITY_THRESHOLDS, BANNED_PHRASES


# =============================================================================
# Document Generation System Prompt
# =============================================================================

DOCUMENT_GENERATION_SYSTEM_PROMPT = """你是一个专业的企业文档助手。

## 必须遵守的底线（Guardrails）

以下是必须遵守的硬性要求，违反会导致输出被拒绝：

1. **最低内容要求**：{min_words} 字
2. **最少章节数量**：{min_sections} 个
3. **禁止使用的表达**：{banned_phrases}
4. 每个观点必须有具体解释和案例
5. 不允许省略或敷衍

## 你的自由空间（Agent决策）

在满足上述底线的前提下，你可以自由决定：

- 文档的具体结构和组织方式
- 论证的逻辑和角度
- 案例和数据的选择
- 语言风格和表达方式
- 是否需要搜索更多信息
- 是否需要分步骤完成

## 工作方式

1. 首先分析任务，理解用户需求
2. 制定内容大纲
3. 逐章节生成详细内容
4. 自我检查是否满足底线
5. 如有问题，自行修复

开始工作。"""


# =============================================================================
# Outline Generation Prompt
# =============================================================================

OUTLINE_GENERATION_PROMPT = """为以下请求创建内容大纲：

**用户请求**：{request}
**文档类型**：{doc_type}

## 必须遵守的底线

- 至少 {min_sections} 个章节
- 必须包含：引言/背景、主体内容、结论/总结
- 每个章节标题清晰明确

## 你的自由空间

- 章节的具体数量（在最低要求之上）
- 章节的具体标题
- 内容的组织逻辑
- 是否需要额外的附录或参考

## 输出格式

```json
{{
    "title": "文档标题",
    "sections": ["章节1标题", "章节2标题", ...]
}}
```"""


# =============================================================================
# Section Generation Prompt
# =============================================================================

SECTION_GENERATION_PROMPT = """为文档 "{doc_title}" 撰写 "{section_title}" 章节。

**文档大纲**：{outline}
**已完成章节**：{completed_sections}

## 必须遵守的底线

- 至少 {min_words_per_section} 字
- 不使用模糊表达：{banned_phrases}
- 内容具体、有深度
- 有实际的例子或数据支撑

## 你的自由空间

- 具体的论述方式和结构
- 使用什么例子
- 语言风格（正式/通俗）
- 是否需要小标题划分

直接输出章节内容，不需要重复标题。"""


# =============================================================================
# Repair Prompt
# =============================================================================

REPAIR_PROMPT = """你生成的内容存在以下质量问题需要修复：

{issues}

**原内容**：
{content}

## 必须修复

- 针对每个问题进行具体修复
- 修复后必须通过质量检查

## 你的自由空间

- 选择如何修复（扩展/重写/补充）
- 保持还是调整原有风格
- 添加什么内容来达标

## 要求

1. 保持原有有价值的内容
2. 不要删除已有的好内容
3. 确保修复后更加充实
4. 输出完整的修复后内容"""


# =============================================================================
# Prompt Builder Functions
# =============================================================================

def build_generation_prompt(
    doc_type: DocumentType,
    custom_thresholds: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build document generation system prompt with guardrails.

    Args:
        doc_type: Document type
        custom_thresholds: Override default thresholds

    Returns:
        Formatted system prompt
    """
    thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    if custom_thresholds:
        thresholds = {**thresholds, **custom_thresholds}

    min_words = thresholds.get("min_words", thresholds.get("min_words_total", 500))
    min_sections = thresholds.get("min_sections", 4)

    # Format banned phrases for display
    banned_display = "、".join(BANNED_PHRASES[:5])
    if len(BANNED_PHRASES) > 5:
        banned_display += " 等"

    return DOCUMENT_GENERATION_SYSTEM_PROMPT.format(
        min_words=min_words,
        min_sections=min_sections,
        banned_phrases=banned_display,
    )


def build_outline_prompt(
    request: str,
    doc_type: DocumentType,
    custom_thresholds: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build outline generation prompt.

    Args:
        request: User's request
        doc_type: Document type
        custom_thresholds: Override default thresholds

    Returns:
        Formatted outline prompt
    """
    thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    if custom_thresholds:
        thresholds = {**thresholds, **custom_thresholds}

    min_sections = thresholds.get("min_sections", 4)

    return OUTLINE_GENERATION_PROMPT.format(
        request=request,
        doc_type=doc_type.value,
        min_sections=min_sections,
    )


def build_section_prompt(
    doc_title: str,
    section_title: str,
    outline: List[str],
    completed_sections: List[str],
    doc_type: DocumentType,
) -> str:
    """
    Build section generation prompt.

    Args:
        doc_title: Document title
        section_title: Section to generate
        outline: Full outline
        completed_sections: Already completed sections
        doc_type: Document type

    Returns:
        Formatted section prompt
    """
    thresholds = QUALITY_THRESHOLDS.get(doc_type, {})
    min_words_per_section = thresholds.get("min_words_per_section", 150)

    # Format banned phrases
    banned_display = "、".join(BANNED_PHRASES[:3])

    return SECTION_GENERATION_PROMPT.format(
        doc_title=doc_title,
        section_title=section_title,
        outline=", ".join(outline),
        completed_sections=", ".join(completed_sections) if completed_sections else "无",
        min_words_per_section=min_words_per_section,
        banned_phrases=banned_display,
    )


def build_repair_prompt(
    content: str,
    issues: List[Dict[str, Any]],
) -> str:
    """
    Build repair prompt for fixing quality issues.

    Args:
        content: Original content
        issues: Quality issues to fix

    Returns:
        Formatted repair prompt
    """
    issues_text = "\n".join(
        f"- [{issue.get('severity', 'warning')}] {issue.get('message', '')}"
        for issue in issues
    )

    return REPAIR_PROMPT.format(
        issues=issues_text,
        content=content,
    )
