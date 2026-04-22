"""
Scenario Analyzer - Intelligent Scenario Detection and Analysis.

This module enables the AI assistant to:
1. Detect user scenario types from queries
2. Build knowledge retrieval strategies based on scenario
3. Generate multi-dimensional expert analysis
4. Combine KB results with expert templates for comprehensive responses

Designed to make the assistant "Manus-like" - an all-knowing problem solver.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger
from ..prompts.scenario_analysis_prompts import (
    SCENARIO_TYPES,
    build_analysis_prompt,
    build_document_analysis_prompt,
    build_document_qa_prompt,
    build_kb_enhanced_prompt,
    build_scenario_detection_prompt,
)

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class ScenarioType(str, Enum):
    """Scenario type enumeration."""

    CUSTOMER_SERVICE = "customer_service"
    SALES_CONSULTATION = "sales_consultation"
    TECHNICAL_SUPPORT = "technical_support"
    PRODUCT_INQUIRY = "product_inquiry"
    POLICY_INQUIRY = "policy_inquiry"
    DATA_ANALYSIS = "data_analysis"
    GENERAL_INQUIRY = "general_inquiry"


class Urgency(str, Enum):
    """Urgency level enumeration."""

    URGENT = "urgent"
    NORMAL = "normal"
    LOW = "low"


@dataclass
class ScenarioDetectionResult:
    """Result of scenario detection."""

    primary_scenario: ScenarioType
    secondary_scenarios: list[ScenarioType] = field(default_factory=list)
    entities: dict[str, str] = field(default_factory=dict)
    urgency: Urgency = Urgency.NORMAL
    requires_kb_search: bool = True
    suggested_kb_queries: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw_response: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "primary_scenario": self.primary_scenario.value,
            "secondary_scenarios": [s.value for s in self.secondary_scenarios],
            "entities": self.entities,
            "urgency": self.urgency.value,
            "requires_kb_search": self.requires_kb_search,
            "suggested_kb_queries": self.suggested_kb_queries,
            "confidence": self.confidence,
        }


@dataclass
class AnalysisContext:
    """Context for analysis generation."""

    user_query: str
    scenario: ScenarioDetectionResult
    kb_context: str = ""
    document_content: str = ""
    additional_context: dict[str, Any] = field(default_factory=dict)


class ScenarioAnalyzer:
    """
    Intelligent scenario analyzer for enterprise AI assistant.

    Features:
    - Rule-based fast detection with keyword matching
    - LLM-based deep analysis for complex scenarios
    - Multi-dimensional expert response generation
    - KB integration for context-aware responses

    Usage:
        ```python
        analyzer = ScenarioAnalyzer(llm_client)

        # Detect scenario
        result = await analyzer.detect_scenario("产品无法正常启动怎么办？")

        # Generate analysis
        analysis = await analyzer.generate_analysis(
            user_query="产品无法正常启动怎么办？",
            scenario=result,
            kb_context="从知识库检索的相关内容..."
        )
        ```
    """

    def __init__(
        self,
        llm_client: Any = None,
        model_name: str = "qwen3.6-plus",
    ):
        """
        Initialize the scenario analyzer.

        Args:
            llm_client: LLM client for deep analysis (optional for rule-based detection)
            model_name: Model name for LLM analysis
        """
        self.llm_client = llm_client
        self.model_name = model_name

    def detect_scenario_fast(self, query: str) -> ScenarioDetectionResult:
        """
        Fast scenario detection using keyword matching.

        This is a lightweight method that doesn't require LLM calls.
        Suitable for real-time scenario routing.

        Args:
            query: User query

        Returns:
            ScenarioDetectionResult with detected scenario
        """
        query_lower = query.lower()
        scores: dict[str, float] = {}

        # Score each scenario based on keyword matches
        for scenario_code, scenario_info in SCENARIO_TYPES.items():
            keywords = scenario_info.get("keywords", [])
            if not keywords:
                continue

            matches = sum(1 for kw in keywords if kw in query_lower)
            if matches > 0:
                scores[scenario_code] = matches / len(keywords)

        # Sort by score
        sorted_scenarios = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        if sorted_scenarios:
            primary = sorted_scenarios[0]
            secondary = [ScenarioType(s[0]) for s in sorted_scenarios[1:3] if s[1] > 0.1]

            # Intelligent KB search decision (Self-RAG style)
            requires_kb = self._should_require_kb_search(query, primary[0])

            return ScenarioDetectionResult(
                primary_scenario=ScenarioType(primary[0]),
                secondary_scenarios=secondary,
                urgency=self._detect_urgency(query),
                requires_kb_search=requires_kb,
                suggested_kb_queries=self._generate_kb_queries(query, primary[0])
                if requires_kb
                else [],
                confidence=min(primary[1] * 2, 1.0),  # Scale confidence
            )

        # Default to general inquiry - also apply smart KB decision
        requires_kb = self._should_require_kb_search(query, "general_inquiry")
        return ScenarioDetectionResult(
            primary_scenario=ScenarioType.GENERAL_INQUIRY,
            urgency=self._detect_urgency(query),
            requires_kb_search=requires_kb,
            suggested_kb_queries=[query] if requires_kb else [],
            confidence=0.5,
        )

    async def detect_scenario_deep(self, query: str) -> ScenarioDetectionResult:
        """
        Deep scenario detection using LLM.

        Provides more accurate detection with entity extraction
        and detailed analysis.

        Args:
            query: User query

        Returns:
            ScenarioDetectionResult with detailed detection
        """
        if not self.llm_client:
            logger.warning("No LLM client provided, falling back to fast detection")
            return self.detect_scenario_fast(query)

        prompt = build_scenario_detection_prompt(query)

        try:
            response = await self.llm_client.generate(
                prompt=prompt,
                model=self.model_name,
                temperature=0.3,  # Lower temperature for consistent detection
            )

            # Parse JSON from response
            result = self._parse_detection_response(response)
            result.raw_response = response
            return result

        except Exception as e:
            logger.error(f"LLM scenario detection failed: {e}, falling back to fast detection")
            return self.detect_scenario_fast(query)

    def _should_require_kb_search(self, query: str, scenario_type: str | None) -> bool:
        """
        Determine if knowledge base search is needed.

        This implements Self-RAG style adaptive retrieval decision:
        1. General AI/technical knowledge → No KB needed (use model knowledge)
        2. Enterprise-specific content → KB needed
        3. Uncertain → Default to KB search (conservative)

        Design Philosophy (Manus-inspired):
        - Don't waste retrieval on queries the model can answer directly
        - Ensure enterprise-specific content always gets KB context
        - Log decisions for observability

        Args:
            query: User query
            scenario_type: Detected scenario type (optional)

        Returns:
            True if KB search should be performed
        """
        query_lower = query.lower()

        # =================================================================
        # Tier 1: Patterns that clearly DON'T need KB search
        # =================================================================

        # 1.1 Greetings and social interactions
        greeting_patterns = [
            r"^(你好|早上好|晚上好|下午好|嗨|hi|hello|hey|good morning|good evening)[\s,，.。!！?？]*$",
            r"^(谢谢|感谢|thanks|thank you)[\s,，.。!！]*$",
            r"^(再见|拜拜|bye|goodbye)[\s,，.。!！]*$",
        ]
        for pattern in greeting_patterns:
            if re.match(pattern, query_lower):
                logger.debug(f"[KB SKIP] Greeting detected: '{query[:30]}...'")
                return False

        # 1.2 Questions about the AI assistant itself
        system_capability_patterns = [
            r"(你能做什么|你的功能|你支持什么|你会什么|你可以做什么)",
            r"(你是谁|你叫什么|你的名字|介绍一下你自己|介绍下你自己)",
            r"(怎么使用你|如何使用你|怎么用你|如何用你)",
        ]
        for pattern in system_capability_patterns:
            if re.search(pattern, query_lower):
                logger.debug(f"[KB SKIP] System capability query: '{query[:30]}...'")
                return False

        # 1.3 General AI/ML/Tech knowledge questions
        # These are well within the model's training knowledge
        general_ai_patterns = [
            # Conceptual questions about AI/ML
            r"(什么是|解释一下|介绍一下|讲解一下).*(机器学习|深度学习|神经网络|人工智能|ai|ml)",
            r"(什么是|解释一下|介绍一下|讲解一下).*(transformer|attention|bert|gpt|llm|大模型|大语言模型)",
            r"(什么是|解释一下|介绍一下|讲解一下).*(flow matching|diffusion|扩散模型|生成模型|gan|vae)",
            r"(什么是|解释一下|介绍一下|讲解一下).*(embedding|向量|词向量|word2vec|sentence)",
            r"(什么是|解释一下|介绍一下|讲解一下).*(rag|检索增强|retrieval|向量数据库)",
            r"(什么是|解释一下|介绍一下|讲解一下).*(agent|智能体|multi-agent|多智能体)",
            # How it works questions
            r"(如何理解|原理是什么|怎么工作|怎么运作|工作原理).*(算法|模型|网络|架构)",
            r"(原理|机制|过程|步骤).*(训练|推理|生成|预测)",
            # Comparison questions about AI concepts
            r"(区别|不同|差异|比较).*(模型|算法|方法|架构)",
            # Programming/coding general knowledge
            r"(什么是|解释一下).*(python|javascript|typescript|java|c\+\+|rust|go语言)",
            r"(什么是|解释一下).*(api|rest|graphql|微服务|容器|docker|kubernetes)",
            r"(什么是|解释一下).*(数据库|sql|nosql|redis|mongodb|postgresql)",
        ]
        for pattern in general_ai_patterns:
            if re.search(pattern, query_lower):
                logger.debug(f"[KB SKIP] General AI knowledge query: '{query[:30]}...'")
                return False

        # =================================================================
        # Tier 2: Patterns that clearly NEED KB search
        # =================================================================

        # 2.1 Enterprise-specific content
        enterprise_patterns = [
            # Company/organization references
            r"(我们公司|公司的|本公司|贵公司|集团|总部)",
            r"(内部|内网|私有|专有)",
            # Business processes and policies
            r"(流程|政策|制度|规范|标准|规定)",
            r"(审批|报销|请假|考勤|绩效)",
            # Products and services
            r"(产品|服务|套餐|方案|报价|定价)",
            r"(功能|特性|版本|更新|升级)",
            # Customer/order related
            r"(客户|订单|合同|账户|账号)",
            r"(购买|下单|付款|退款|售后)",
            # Documentation references
            r"(文档|手册|指南|教程|说明书)",
            r"(操作指南|使用说明|配置说明)",
            # Historical/contextual queries
            r"(之前|上次|历史|记录)",
        ]
        for pattern in enterprise_patterns:
            if re.search(pattern, query_lower):
                logger.debug(f"[KB NEED] Enterprise-specific query: '{query[:30]}...'")
                return True

        # =================================================================
        # Tier 3: Scenario-based decision
        # =================================================================

        # Scenarios that typically need KB
        kb_required_scenarios = {
            "customer_service",  # Customer issues need company policies
            "sales_consultation",  # Sales needs product info
            "technical_support",  # Support needs documentation
            "product_inquiry",  # Product info from KB
            "policy_inquiry",  # Policy definitely needs KB
        }

        # Scenarios that might not need KB
        kb_optional_scenarios = {
            "general_inquiry",  # General questions may use model knowledge
            "data_analysis",  # Depends on whether it's about internal data
        }

        if scenario_type in kb_required_scenarios:
            # But if it's a very short, simple query, might still skip
            if len(query) < 30 and not any(
                kw in query_lower for kw in ["文档", "产品", "公司", "我们", "政策"]
            ):
                # Short query without enterprise keywords - might be general
                logger.debug(f"[KB SKIP] Short query in KB scenario: '{query}'")
                return False
            return True

        if scenario_type in kb_optional_scenarios:
            # For general inquiry, default to no KB unless there are enterprise signals
            if len(query) < 50:
                logger.debug(f"[KB SKIP] Short general inquiry: '{query}'")
                return False

        # =================================================================
        # Tier 4: Default - Conservative strategy
        # =================================================================
        # If we're unsure, better to search than to miss relevant info
        logger.debug(f"[KB DEFAULT] Defaulting to KB search: '{query[:30]}...'")
        return True

    def _detect_urgency(self, query: str) -> Urgency:
        """Detect urgency level from query."""
        urgent_keywords = ["紧急", "急", "马上", "立即", "现在", "尽快", "严重", "崩溃", "无法使用"]
        low_keywords = ["想了解", "咨询一下", "请问", "好奇", "顺便"]

        query_lower = query.lower()

        if any(kw in query_lower for kw in urgent_keywords):
            return Urgency.URGENT
        elif any(kw in query_lower for kw in low_keywords):
            return Urgency.LOW
        return Urgency.NORMAL

    def _generate_kb_queries(self, query: str, scenario_type: str) -> list[str]:
        """Generate suggested KB search queries based on scenario."""
        queries = [query]  # Original query is always included

        scenario_info = SCENARIO_TYPES.get(scenario_type, {})
        keywords = scenario_info.get("keywords", [])

        # Find keywords present in query
        matched_keywords = [kw for kw in keywords if kw in query]

        # Generate variant queries
        if matched_keywords:
            # Add keyword-focused query
            queries.append(" ".join(matched_keywords))

        # Add scenario-specific query patterns
        if scenario_type == "technical_support":
            queries.append(f"解决 {query}")
            queries.append(f"故障排除 {query}")
        elif scenario_type == "product_inquiry":
            queries.append(f"产品功能 {query}")
        elif scenario_type == "customer_service":
            queries.append(f"处理方案 {query}")

        return queries[:5]  # Limit to 5 queries

    def _parse_detection_response(self, response: str) -> ScenarioDetectionResult:
        """Parse LLM detection response."""
        # Try to extract JSON from response
        json_match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find JSON object directly
            json_match = re.search(r"\{[^{}]*\}", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                # Fallback to fast detection
                return ScenarioDetectionResult(
                    primary_scenario=ScenarioType.GENERAL_INQUIRY,
                    confidence=0.3,
                )

        try:
            data = json.loads(json_str)

            primary = data.get("primary_scenario", "general_inquiry")
            # Validate scenario type
            try:
                primary_scenario = ScenarioType(primary)
            except ValueError:
                primary_scenario = ScenarioType.GENERAL_INQUIRY

            secondary = data.get("secondary_scenarios", [])
            secondary_scenarios = []
            for s in secondary:
                with contextlib.suppress(ValueError):
                    secondary_scenarios.append(ScenarioType(s))

            urgency_str = data.get("urgency", "normal")
            try:
                urgency = Urgency(urgency_str)
            except ValueError:
                urgency = Urgency.NORMAL

            return ScenarioDetectionResult(
                primary_scenario=primary_scenario,
                secondary_scenarios=secondary_scenarios,
                entities=data.get("entities", {}),
                urgency=urgency,
                requires_kb_search=data.get("requires_kb_search", True),
                suggested_kb_queries=data.get("suggested_kb_queries", []),
                confidence=data.get("confidence", 0.7),
            )

        except json.JSONDecodeError:
            return ScenarioDetectionResult(
                primary_scenario=ScenarioType.GENERAL_INQUIRY,
                confidence=0.3,
            )

    def build_analysis_prompt(
        self,
        user_query: str,
        scenario: ScenarioDetectionResult,
        kb_context: str = "",
        document_content: str = "",
    ) -> str:
        """
        Build analysis prompt based on detected scenario.

        Args:
            user_query: User's question
            scenario: Detected scenario
            kb_context: Retrieved KB context
            document_content: Uploaded document content (if any)

        Returns:
            Formatted analysis prompt
        """
        # If we have both KB and document content, use enhanced prompt
        if kb_context and document_content:
            return build_kb_enhanced_prompt(
                user_query=user_query,
                kb_results=kb_context,
                document_content=document_content,
            )

        # If we have document content but user has a question about it
        if document_content and user_query:
            return build_document_qa_prompt(
                document_content=document_content,
                user_query=user_query,
            )

        # Standard scenario-based analysis
        return build_analysis_prompt(
            user_query=user_query,
            scenario_type=scenario.primary_scenario.value,
            kb_context=kb_context,
        )

    def get_scenario_info(self, scenario_type: ScenarioType) -> dict[str, Any]:
        """Get information about a scenario type."""
        return SCENARIO_TYPES.get(scenario_type.value, SCENARIO_TYPES["general_inquiry"])

    def get_analysis_dimensions(self, scenario_type: ScenarioType) -> list[str]:
        """Get analysis dimensions for a scenario type."""
        info = self.get_scenario_info(scenario_type)
        return info.get("analysis_dimensions", [])


# =============================================================================
# Document Analyzer
# =============================================================================


class DocumentAnalyzer:
    """
    Document analyzer for deep document understanding.

    Features:
    - Structure analysis
    - Key information extraction
    - Content summarization
    - KB correlation analysis

    Usage:
        ```python
        analyzer = DocumentAnalyzer(llm_client)

        # Analyze document
        analysis = await analyzer.analyze(document_content)

        # Answer questions about document
        answer = await analyzer.answer_question(document_content, "文档的主要结论是什么？")
        ```
    """

    def __init__(
        self,
        llm_client: Any = None,
        model_name: str = "qwen3.6-plus",
    ):
        """
        Initialize the document analyzer.

        Args:
            llm_client: LLM client for analysis
            model_name: Model name for analysis
        """
        self.llm_client = llm_client
        self.model_name = model_name

    async def analyze(
        self,
        document_content: str,
        analysis_task: str = "全面分析文档内容",
    ) -> str:
        """
        Perform deep analysis on document.

        Args:
            document_content: Document content to analyze
            analysis_task: Specific analysis task

        Returns:
            Analysis result
        """
        if not self.llm_client:
            raise ValueError("LLM client required for document analysis")

        prompt = build_document_analysis_prompt(
            document_content=document_content,
            analysis_task=analysis_task,
        )

        response = await self.llm_client.generate(
            prompt=prompt,
            model=self.model_name,
            temperature=0.5,
        )

        return response

    async def answer_question(
        self,
        document_content: str,
        question: str,
    ) -> str:
        """
        Answer a question based on document content.

        Args:
            document_content: Document content
            question: User's question

        Returns:
            Answer based on document
        """
        if not self.llm_client:
            raise ValueError("LLM client required for document QA")

        prompt = build_document_qa_prompt(
            document_content=document_content,
            user_query=question,
        )

        response = await self.llm_client.generate(
            prompt=prompt,
            model=self.model_name,
            temperature=0.5,
        )

        return response

    def extract_structure(self, content: str) -> dict[str, Any]:
        """
        Extract document structure (lightweight, no LLM).

        Args:
            content: Document content

        Returns:
            Structure information
        """
        lines = content.split("\n")
        structure = {
            "total_lines": len(lines),
            "total_chars": len(content),
            "sections": [],
            "has_headers": False,
            "has_lists": False,
            "has_tables": False,
        }

        # Detect headers (Markdown style)
        for i, line in enumerate(lines):
            if line.startswith("#"):
                structure["has_headers"] = True
                level = len(line) - len(line.lstrip("#"))
                title = line.lstrip("#").strip()
                structure["sections"].append(
                    {
                        "level": level,
                        "title": title,
                        "line": i + 1,
                    }
                )

        # Detect lists
        if any(line.strip().startswith(("-", "*", "1.", "2.")) for line in lines):
            structure["has_lists"] = True

        # Detect tables
        if any("|" in line for line in lines):
            structure["has_tables"] = True

        return structure


# =============================================================================
# Factory Functions
# =============================================================================


def create_scenario_analyzer(
    llm_client: Any = None,
    model_name: str = "qwen3.6-plus",
) -> ScenarioAnalyzer:
    """Create a scenario analyzer instance."""
    return ScenarioAnalyzer(llm_client=llm_client, model_name=model_name)


def create_document_analyzer(
    llm_client: Any = None,
    model_name: str = "qwen3.6-plus",
) -> DocumentAnalyzer:
    """Create a document analyzer instance."""
    return DocumentAnalyzer(llm_client=llm_client, model_name=model_name)
