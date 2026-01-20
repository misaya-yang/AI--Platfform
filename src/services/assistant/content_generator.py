"""
Deep Content Generator for Enterprise Agent.

Generates high-quality content through a structured multi-phase process:
1. Outline Generation - Create content structure
2. Section-by-Section Generation - Generate each section with depth
3. Quality Validation - Check against guardrails
4. Self-Repair - Agent autonomously fixes issues

The Agent operates freely within guardrail boundaries, deciding:
- Content structure and organization
- Writing style and tone
- How to fix quality issues

References:
- Manus Context Engineering: https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from ...core.observability.logging import get_logger
from .guardrails import (
    DocumentType,
    QualityGuardrails,
    QualityIssue,
    ValidationResult,
)

logger = get_logger(__name__)


class GenerationPhase(str, Enum):
    """Phases of content generation."""

    PLANNING = "planning"
    OUTLINE = "outline"
    GENERATING = "generating"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    COMPLETE = "complete"


@dataclass
class ContentSection:
    """A section of generated content."""

    title: str
    content: str
    word_count: int = 0

    def __post_init__(self):
        if self.word_count == 0:
            self.word_count = len(self.content)


@dataclass
class ContentOutline:
    """Outline structure for content generation."""

    title: str
    sections: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedContent:
    """Result of content generation."""

    content: str
    sections: List[ContentSection]
    outline: Optional[ContentOutline] = None
    validation: Optional[ValidationResult] = None
    repair_attempts: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        """Total word count of all sections."""
        return sum(s.word_count for s in self.sections)


@dataclass
class StreamEvent:
    """Event emitted during streaming generation."""

    event_type: str
    data: Any
    phase: GenerationPhase = GenerationPhase.GENERATING


class DeepContentGenerator:
    """
    Deep content generator with multi-phase generation and guardrail validation.

    Agent decides:
    - Content structure (outline)
    - Writing style and depth
    - How to fix quality issues

    Guardrails enforce:
    - Minimum word counts
    - Required sections
    - Banned phrases

    Usage:
        generator = DeepContentGenerator(llm_client, guardrails)

        async for event in generator.generate(
            task=ContentTask(
                request="写一份市场分析报告",
                doc_type=DocumentType.DOCX,
            )
        ):
            if event.event_type == "text_delta":
                print(event.data, end="")
    """

    MAX_REPAIR_ATTEMPTS = 3

    def __init__(
        self,
        llm_client: Any,
        guardrails: Optional[QualityGuardrails] = None,
        model_name: str = "claude-3-haiku-20240307",
    ):
        """
        Initialize the generator.

        Args:
            llm_client: LLM client for content generation
            guardrails: Quality guardrails validator
            model_name: Model to use for generation
        """
        self.llm_client = llm_client
        self.guardrails = guardrails or QualityGuardrails()
        self.model_name = model_name

    async def generate(
        self,
        request: str,
        doc_type: DocumentType,
        context: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        Generate content through multi-phase process.

        Phases:
        1. Generate outline (Agent decides structure)
        2. Generate each section (Agent decides content)
        3. Validate against guardrails (hardcoded checks)
        4. Self-repair if needed (Agent decides how to fix)

        Args:
            request: User's content request
            doc_type: Type of document to generate
            context: Additional context (e.g., reference materials)

        Yields:
            StreamEvent with generation progress and content
        """
        context = context or {}

        # Phase 1: Planning
        yield StreamEvent(
            event_type="status",
            data={"message": "正在规划内容结构...", "phase": "planning"},
            phase=GenerationPhase.PLANNING,
        )

        # Phase 2: Generate outline
        yield StreamEvent(
            event_type="status",
            data={"message": "生成内容大纲...", "phase": "outline"},
            phase=GenerationPhase.OUTLINE,
        )

        outline = await self._generate_outline(request, doc_type, context)

        yield StreamEvent(
            event_type="outline",
            data={"title": outline.title, "sections": outline.sections},
            phase=GenerationPhase.OUTLINE,
        )

        # Phase 3: Generate sections
        yield StreamEvent(
            event_type="status",
            data={"message": "生成详细内容...", "phase": "generating"},
            phase=GenerationPhase.GENERATING,
        )

        sections: List[ContentSection] = []
        full_content = ""

        for i, section_title in enumerate(outline.sections):
            yield StreamEvent(
                event_type="section_start",
                data={"index": i, "title": section_title},
                phase=GenerationPhase.GENERATING,
            )

            section_content = ""
            async for chunk in self._generate_section(
                section_title=section_title,
                outline=outline,
                doc_type=doc_type,
                context=context,
                previous_sections=sections,
            ):
                section_content += chunk
                yield StreamEvent(
                    event_type="text_delta",
                    data=chunk,
                    phase=GenerationPhase.GENERATING,
                )

            sections.append(ContentSection(
                title=section_title,
                content=section_content,
            ))
            full_content += f"\n\n# {section_title}\n\n{section_content}"

        # Phase 4: Validation
        yield StreamEvent(
            event_type="status",
            data={"message": "验证内容质量...", "phase": "validating"},
            phase=GenerationPhase.VALIDATING,
        )

        validation = self.guardrails.validate(full_content, doc_type)

        yield StreamEvent(
            event_type="validation",
            data={
                "passed": validation.passed,
                "score": validation.score,
                "issues": [i.to_dict() for i in validation.issues],
            },
            phase=GenerationPhase.VALIDATING,
        )

        # Phase 5: Self-repair if needed
        repair_attempts = 0
        while not validation.passed and repair_attempts < self.MAX_REPAIR_ATTEMPTS:
            repair_attempts += 1

            yield StreamEvent(
                event_type="status",
                data={
                    "message": f"检测到质量问题，正在修复 ({repair_attempts}/{self.MAX_REPAIR_ATTEMPTS})...",
                    "phase": "repairing",
                },
                phase=GenerationPhase.REPAIRING,
            )

            # Agent self-repair
            repaired_content = ""
            async for chunk in self._repair_content(
                content=full_content,
                issues=validation.issues,
                doc_type=doc_type,
            ):
                repaired_content += chunk
                yield StreamEvent(
                    event_type="repair_delta",
                    data=chunk,
                    phase=GenerationPhase.REPAIRING,
                )

            full_content = repaired_content

            # Re-validate
            validation = self.guardrails.validate(full_content, doc_type)

            yield StreamEvent(
                event_type="validation",
                data={
                    "passed": validation.passed,
                    "score": validation.score,
                    "issues": [i.to_dict() for i in validation.issues],
                    "attempt": repair_attempts,
                },
                phase=GenerationPhase.VALIDATING,
            )

        # Complete
        yield StreamEvent(
            event_type="complete",
            data={
                "content": full_content,
                "word_count": self._count_words(full_content),
                "validation_passed": validation.passed,
                "repair_attempts": repair_attempts,
            },
            phase=GenerationPhase.COMPLETE,
        )

    async def _generate_outline(
        self,
        request: str,
        doc_type: DocumentType,
        context: Dict[str, Any],
    ) -> ContentOutline:
        """
        Generate content outline.

        Agent decides:
        - Number of sections
        - Section titles
        - Content structure

        Args:
            request: User's request
            doc_type: Document type
            context: Additional context

        Returns:
            ContentOutline with structure
        """
        # Get guardrail requirements
        thresholds = self.guardrails.thresholds.get(doc_type, {})
        min_sections = thresholds.get("min_sections", 4)

        prompt = f"""为以下请求创建内容大纲：

请求：{request}
文档类型：{doc_type.value}

要求：
- 至少 {min_sections} 个章节
- 每个章节标题清晰明确
- 结构合理，逻辑通顺

输出JSON格式：
```json
{{
    "title": "文档标题",
    "sections": ["章节1标题", "章节2标题", ...]
}}
```"""

        response = await self._call_llm(prompt)

        try:
            # Extract JSON from response
            import re
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', response)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                data = json.loads(response)

            return ContentOutline(
                title=data.get("title", request[:50]),
                sections=data.get("sections", [f"章节 {i+1}" for i in range(min_sections)]),
            )
        except (json.JSONDecodeError, KeyError):
            # Fallback to default structure
            return ContentOutline(
                title=request[:50],
                sections=[f"章节 {i+1}" for i in range(min_sections)],
            )

    async def _generate_section(
        self,
        section_title: str,
        outline: ContentOutline,
        doc_type: DocumentType,
        context: Dict[str, Any],
        previous_sections: List[ContentSection],
    ) -> AsyncIterator[str]:
        """
        Generate a single section with depth.

        Agent decides:
        - Content and details
        - Writing style
        - Examples and evidence

        Args:
            section_title: Title of section to generate
            outline: Full outline
            doc_type: Document type
            context: Additional context
            previous_sections: Previously generated sections

        Yields:
            Content chunks
        """
        thresholds = self.guardrails.thresholds.get(doc_type, {})
        min_words_per_section = thresholds.get("min_words_per_section", 150)

        # Build context from previous sections
        prev_context = ""
        if previous_sections:
            prev_context = "已完成章节：\n" + "\n".join(
                f"- {s.title}" for s in previous_sections
            )

        prompt = f"""为文档 "{outline.title}" 写 "{section_title}" 章节的详细内容。

文档大纲：
{', '.join(outline.sections)}

{prev_context}

要求：
- 内容详实，至少 {min_words_per_section} 字
- 具体解释和案例
- 不使用模糊表达如"等等"、"诸如此类"
- 专业且有深度

直接输出章节内容，不需要标题。"""

        async for chunk in self._stream_llm(prompt):
            yield chunk

    async def _repair_content(
        self,
        content: str,
        issues: List[QualityIssue],
        doc_type: DocumentType,
    ) -> AsyncIterator[str]:
        """
        Self-repair content based on quality issues.

        Agent decides HOW to fix, guardrails define WHAT to fix.

        Args:
            content: Original content
            issues: Quality issues found
            doc_type: Document type

        Yields:
            Repaired content chunks
        """
        issues_text = "\n".join(
            f"- [{i.severity.value}] {i.message} (建议: {i.action})"
            for i in issues
        )

        prompt = f"""你生成的内容存在以下问题需要修复：

{issues_text}

原内容：
{content}

请修复上述问题，输出完整的修复后内容。
注意：
1. 保持原有结构和风格
2. 针对每个问题进行具体修复
3. 不要删除原有的有价值内容
4. 确保修复后内容更加充实详细"""

        async for chunk in self._stream_llm(prompt):
            yield chunk

    async def _call_llm(self, prompt: str) -> str:
        """
        Call LLM and return full response.

        Args:
            prompt: Prompt to send

        Returns:
            LLM response text
        """
        if hasattr(self.llm_client, 'messages'):
            # Anthropic
            response = await self.llm_client.messages.create(
                model=self.model_name,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        if hasattr(self.llm_client, 'chat'):
            # OpenAI-compatible
            response = await self.llm_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
            )
            return response.choices[0].message.content

        raise ValueError("Unsupported LLM client")

    async def _stream_llm(self, prompt: str) -> AsyncIterator[str]:
        """
        Stream LLM response.

        Args:
            prompt: Prompt to send

        Yields:
            Response chunks
        """
        if hasattr(self.llm_client, 'messages'):
            # Anthropic streaming
            async with self.llm_client.messages.stream(
                model=self.model_name,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        elif hasattr(self.llm_client, 'chat'):
            # OpenAI-compatible streaming
            stream = await self.llm_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        else:
            # Fallback to non-streaming
            response = await self._call_llm(prompt)
            yield response

    def _count_words(self, content: str) -> int:
        """Count words in content."""
        import re
        clean = re.sub(r'[#*`~\[\](){}|]', '', content)
        clean = re.sub(r'\s+', ' ', clean).strip()
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', clean))
        english_words = len(re.findall(r'[a-zA-Z]+', clean))
        return chinese_chars + english_words


def create_content_generator(
    llm_client: Any,
    guardrails: Optional[QualityGuardrails] = None,
    model_name: str = "claude-3-haiku-20240307",
) -> DeepContentGenerator:
    """
    Factory function to create a DeepContentGenerator.

    Args:
        llm_client: LLM client
        guardrails: Quality guardrails
        model_name: Model to use

    Returns:
        Configured DeepContentGenerator
    """
    return DeepContentGenerator(
        llm_client=llm_client,
        guardrails=guardrails,
        model_name=model_name,
    )
