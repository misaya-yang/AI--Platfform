"""
Scenario Analysis Prompts for Enterprise AI Assistant.

These prompts enable the AI to:
1. Identify user scenario types (customer service, sales, technical support, etc.)
2. Build knowledge retrieval strategies based on scenario
3. Generate multi-dimensional analysis (diagnosis, causes, solutions, notes)
4. Provide expert-level response frameworks

Designed to make the assistant "Manus-like" - an all-knowing problem solver.
"""

from typing import Any, Dict, List, Optional


# =============================================================================
# Scenario Types Definition
# =============================================================================

SCENARIO_TYPES = {
    "customer_service": {
        "name": "客户服务",
        "description": "处理客户投诉、问题反馈、服务咨询",
        "keywords": ["投诉", "问题", "反馈", "不满", "退款", "售后", "维修", "故障"],
        "analysis_dimensions": ["问题诊断", "情绪安抚", "解决方案", "预防措施"],
    },
    "sales_consultation": {
        "name": "销售咨询",
        "description": "产品推荐、价格咨询、促销活动、购买决策",
        "keywords": ["购买", "价格", "促销", "优惠", "推荐", "选择", "对比", "预算"],
        "analysis_dimensions": ["需求分析", "产品匹配", "价值主张", "购买建议"],
    },
    "technical_support": {
        "name": "技术支持",
        "description": "技术问题、操作指导、故障排除、配置帮助",
        "keywords": ["怎么", "如何", "报错", "无法", "配置", "安装", "升级", "设置"],
        "analysis_dimensions": ["问题识别", "原因分析", "操作步骤", "验证方法"],
    },
    "product_inquiry": {
        "name": "产品咨询",
        "description": "产品功能、规格参数、使用方法、适用场景",
        "keywords": ["功能", "特点", "参数", "规格", "支持", "适用", "区别", "版本"],
        "analysis_dimensions": ["功能说明", "应用场景", "技术规格", "选型建议"],
    },
    "policy_inquiry": {
        "name": "政策咨询",
        "description": "公司政策、规章制度、合规要求、流程说明",
        "keywords": ["政策", "规定", "流程", "要求", "规则", "标准", "合规", "审批"],
        "analysis_dimensions": ["政策解读", "适用条件", "操作流程", "注意事项"],
    },
    "data_analysis": {
        "name": "数据分析",
        "description": "数据解读、趋势分析、报表理解、指标说明",
        "keywords": ["数据", "报表", "指标", "趋势", "分析", "统计", "对比", "增长"],
        "analysis_dimensions": ["数据解读", "趋势分析", "原因探究", "行动建议"],
    },
    "general_inquiry": {
        "name": "通用咨询",
        "description": "一般性问题、信息查询、知识获取",
        "keywords": [],
        "analysis_dimensions": ["信息汇总", "关键要点", "补充说明", "相关参考"],
    },
}


# =============================================================================
# Scenario Detection Prompt
# =============================================================================

SCENARIO_DETECTION_PROMPT = """分析用户问题，识别场景类型和关键信息。

**用户问题**：
{user_query}

**可选场景类型**：
{scenario_types}

**分析要求**：
1. 识别最匹配的场景类型（可以是多个，按匹配度排序）
2. 提取关键实体（产品名、问题点、客户诉求等）
3. 评估问题紧急程度（urgent/normal/low）
4. 判断是否需要检索知识库

**输出JSON格式**：
```json
{{
    "primary_scenario": "场景类型代码",
    "secondary_scenarios": ["次要场景1", "次要场景2"],
    "entities": {{
        "product": "产品名称（如有）",
        "issue": "问题描述",
        "customer_need": "客户核心诉求"
    }},
    "urgency": "urgent/normal/low",
    "requires_kb_search": true/false,
    "suggested_kb_queries": ["建议的知识库搜索词1", "建议的知识库搜索词2"],
    "confidence": 0.0-1.0
}}
```"""


# =============================================================================
# Multi-Dimensional Analysis Prompt
# =============================================================================

MULTI_DIMENSIONAL_ANALYSIS_PROMPT = """你是一位资深的{scenario_name}专家。请基于以下信息，进行多维度专业分析。

## 用户问题
{user_query}

## 相关知识库内容
{kb_context}

## 分析维度
按以下维度进行深度分析：
{analysis_dimensions}

## 分析要求
1. **诊断准确**：准确识别问题的本质和根源
2. **方案实用**：提供可操作的具体建议
3. **表达专业**：使用恰当的专业术语
4. **逻辑清晰**：层次分明，条理清楚
5. **考虑周全**：涵盖可能的边界情况和注意事项

## 输出格式
请按以下结构组织回答：

### 问题理解
[简要复述对用户问题的理解，确认理解无误]

{dimension_sections}

### 总结建议
[给出最终的综合建议，突出最重要的行动项]

### 相关提示
[可能有帮助的额外信息或提醒]
"""


# =============================================================================
# Expert Response Templates
# =============================================================================

EXPERT_TEMPLATES = {
    "customer_service": """### 问题诊断
[识别客户遇到的具体问题，明确问题的表现和影响]

### 情绪回应
[理解客户的感受，给予适当的同理心表达]

### 解决方案
[提供具体可行的解决步骤，包括：
1. 立即可采取的措施
2. 后续处理流程
3. 预计解决时间]

### 预防措施
[建议如何避免类似问题再次发生]""",

    "sales_consultation": """### 需求分析
[理解客户的核心需求和使用场景]

### 产品推荐
[基于需求推荐合适的产品/服务，说明推荐理由：
- 产品特点如何匹配需求
- 性价比分析
- 与其他选择的对比]

### 价值主张
[强调产品能为客户带来的核心价值]

### 购买建议
[提供购买决策建议，包括时机、渠道、注意事项]""",

    "technical_support": """### 问题识别
[明确问题的具体表现和发生条件]

### 原因分析
[分析可能导致问题的原因，按可能性排序]

### 解决步骤
[提供详细的解决操作步骤：
1. 第一步：...
2. 第二步：...
（每步说明操作、预期结果、注意事项）]

### 验证方法
[如何确认问题已解决]

### 注意事项
[操作过程中需要注意的要点]""",

    "product_inquiry": """### 功能概述
[产品/功能的主要介绍]

### 核心特点
[突出的功能特点和优势]

### 应用场景
[适合使用的具体场景]

### 技术规格
[关键的技术参数（如适用）]

### 选型建议
[针对用户情况的具体建议]""",

    "policy_inquiry": """### 政策说明
[相关政策的核心内容]

### 适用范围
[政策适用的条件和对象]

### 执行流程
[按政策要求的具体操作步骤]

### 注意事项
[执行过程中需要注意的要点]

### 常见问题
[相关的常见疑问解答]""",

    "data_analysis": """### 数据解读
[对数据的基本解读和含义说明]

### 趋势分析
[数据呈现的趋势和规律]

### 原因探究
[可能导致这些数据表现的原因]

### 行动建议
[基于数据分析的具体行动建议]""",

    "general_inquiry": """### 信息汇总
[针对问题的核心信息整理]

### 关键要点
[需要重点关注的内容]

### 补充说明
[有助于理解的额外信息]

### 相关参考
[可能有帮助的延伸内容]""",
}


# =============================================================================
# Document Analysis Prompts
# =============================================================================

DOCUMENT_ANALYSIS_PROMPT = """你是一位专业的文档分析专家。请对以下文档进行深度分析。

## 文档内容
{document_content}

## 分析任务
{analysis_task}

## 分析要求
1. **结构分析**：识别文档的组织结构和层次
2. **关键信息提取**：提取最重要的信息点
3. **数据识别**：识别关键数据和指标
4. **观点归纳**：总结文档的核心观点
5. **深度理解**：理解文档的潜在含义和价值

## 输出格式

### 文档概览
[文档类型、主题、篇幅等基本信息]

### 结构分析
[文档的组织结构，列出主要章节/部分]

### 核心内容
[文档的主要内容和关键信息]

### 重要数据
[文档中的关键数据和指标（如有）]

### 观点总结
[文档的核心观点和结论]

### 深度洞察
[文档未直接表述但可以推断的内容]

### 应用建议
[基于文档内容的行动建议或应用方向]
"""


DOCUMENT_QA_PROMPT = """基于以下文档内容回答用户问题。

## 文档内容
{document_content}

## 用户问题
{user_query}

## 回答要求
1. **准确引用**：回答必须基于文档内容，标注信息来源
2. **完整覆盖**：尽可能覆盖文档中与问题相关的所有内容
3. **清晰表达**：条理清晰，易于理解
4. **诚实标注**：如果文档中没有相关信息，明确说明

## 输出格式

### 直接回答
[针对用户问题的直接回答]

### 详细说明
[相关的详细信息和上下文]

### 引用来源
[指出信息在文档中的位置（如适用）]

### 补充信息
[文档中可能有帮助的相关内容]

### 信息边界
[说明文档中未涵盖的相关方面（如有）]
"""


# =============================================================================
# KB-Enhanced Analysis Prompt
# =============================================================================

KB_ENHANCED_ANALYSIS_PROMPT = """你是一位具备企业知识库支持的AI分析师。请结合知识库内容和你的专业能力，为用户提供深度分析。

## 用户问题
{user_query}

## 知识库检索结果
{kb_results}

## 上传文档内容（如有）
{document_content}

## 分析要求

### 第一步：信息整合
- 从知识库和文档中提取相关信息
- 识别信息的可靠性和时效性
- 标注信息来源

### 第二步：深度分析
- 综合各方面信息进行分析
- 识别潜在的问题或机会
- 考虑不同视角和可能性

### 第三步：解决方案
- 提供具体可行的建议
- 说明建议的依据
- 考虑实施的可行性

### 第四步：补充说明
- 指出信息的局限性
- 建议进一步了解的方向
- 提供相关注意事项

## 输出格式

### 问题理解
[对用户问题的理解和确认]

### 相关发现
[从知识库和文档中发现的相关信息，标注来源]

### 深度分析
[基于信息的专业分析]

### 解决方案
[具体可行的建议和步骤]

### 信息说明
[信息来源和可靠性说明]

### 延伸建议
[可能有帮助的额外信息或后续建议]
"""


# =============================================================================
# Prompt Builder Functions
# =============================================================================

def get_scenario_types_description() -> str:
    """Get formatted description of all scenario types."""
    lines = []
    for code, info in SCENARIO_TYPES.items():
        lines.append(f"- **{code}** ({info['name']}): {info['description']}")
    return "\n".join(lines)


def build_scenario_detection_prompt(user_query: str) -> str:
    """Build prompt for scenario detection."""
    return SCENARIO_DETECTION_PROMPT.format(
        user_query=user_query,
        scenario_types=get_scenario_types_description(),
    )


def build_analysis_prompt(
    user_query: str,
    scenario_type: str,
    kb_context: str = "",
) -> str:
    """
    Build multi-dimensional analysis prompt based on scenario type.

    Args:
        user_query: User's question
        scenario_type: Detected scenario type code
        kb_context: Retrieved knowledge base context

    Returns:
        Formatted analysis prompt
    """
    scenario_info = SCENARIO_TYPES.get(scenario_type, SCENARIO_TYPES["general_inquiry"])
    scenario_name = scenario_info["name"]
    dimensions = scenario_info["analysis_dimensions"]

    # Build dimension sections
    dimension_sections = EXPERT_TEMPLATES.get(
        scenario_type,
        EXPERT_TEMPLATES["general_inquiry"]
    )

    # Format dimensions list
    dimensions_text = "\n".join(f"- {dim}" for dim in dimensions)

    return MULTI_DIMENSIONAL_ANALYSIS_PROMPT.format(
        scenario_name=scenario_name,
        user_query=user_query,
        kb_context=kb_context if kb_context else "（无相关知识库内容）",
        analysis_dimensions=dimensions_text,
        dimension_sections=dimension_sections,
    )


def build_document_analysis_prompt(
    document_content: str,
    analysis_task: str = "全面分析文档内容",
) -> str:
    """Build document analysis prompt."""
    return DOCUMENT_ANALYSIS_PROMPT.format(
        document_content=document_content,
        analysis_task=analysis_task,
    )


def build_document_qa_prompt(
    document_content: str,
    user_query: str,
) -> str:
    """Build document QA prompt."""
    return DOCUMENT_QA_PROMPT.format(
        document_content=document_content,
        user_query=user_query,
    )


def build_kb_enhanced_prompt(
    user_query: str,
    kb_results: str,
    document_content: str = "",
) -> str:
    """Build KB-enhanced analysis prompt."""
    return KB_ENHANCED_ANALYSIS_PROMPT.format(
        user_query=user_query,
        kb_results=kb_results,
        document_content=document_content if document_content else "（无上传文档）",
    )
