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
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ....core.observability.logging import get_logger
from ..agent.agui_protocol import create_agui_emitter
from ..quality.guardrails import (
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
    sections: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedContent:
    """Result of content generation."""

    content: str
    sections: list[ContentSection]
    outline: ContentOutline | None = None
    validation: ValidationResult | None = None
    repair_attempts: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

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
        guardrails: QualityGuardrails | None = None,
        model_name: str = "qwen3.6-plus",
    ):
        """
        Initialize the generator.

        Args:
            llm_client: LLM client for content generation
            guardrails: Quality guardrails validator
            model_name: Model to use for generation (default: claude-sonnet-4 for quality)
        """
        self.llm_client = llm_client
        self.guardrails = guardrails or QualityGuardrails()
        self.model_name = model_name

    async def generate(
        self,
        request: str,
        doc_type: DocumentType,
        context: dict[str, Any] | None = None,
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

        sections: list[ContentSection] = []
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

            sections.append(
                ContentSection(
                    title=section_title,
                    content=section_content,
                )
            )
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

    async def generate_agui(
        self,
        request: str,
        doc_type: DocumentType,
        request_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """
        Generate content with AG-UI protocol events.

        This method produces SSE-formatted strings following the AG-UI protocol,
        suitable for direct streaming to frontend clients.

        Args:
            request: User's content request
            doc_type: Type of document to generate
            request_id: Request identifier for event correlation
            context: Additional context

        Yields:
            SSE-formatted event strings
        """
        context = context or {}
        request_id = request_id or str(uuid.uuid4())
        run_id = str(uuid.uuid4())

        # Create AG-UI emitter
        emitter = create_agui_emitter(request_id=request_id, run_id=run_id)

        # Emit run started
        yield emitter.run_started(metadata={"doc_type": doc_type.value})

        # Step 1: Planning
        step_id_planning = str(uuid.uuid4())
        yield emitter.step_started(
            step_name="规划内容结构",
            step_type="planning",
            step_id=step_id_planning,
        )
        yield emitter.status("planning", "正在规划内容结构...", phase="planning")

        # Step 2: Outline Generation
        step_id_outline = str(uuid.uuid4())
        yield emitter.step_finished(step_id=step_id_planning)
        yield emitter.step_started(
            step_name="生成内容大纲",
            step_type="outline",
            step_id=step_id_outline,
        )
        yield emitter.status("outline", "生成内容大纲...", phase="outline")

        outline = await self._generate_outline(request, doc_type, context)

        # Emit outline ready event
        yield emitter.outline_ready(
            title=outline.title,
            sections=outline.sections,
            metadata={"doc_type": doc_type.value},
        )
        yield emitter.step_finished(step_id=step_id_outline)

        # Step 3: Generate sections
        step_id_generating = str(uuid.uuid4())
        yield emitter.step_started(
            step_name="生成详细内容",
            step_type="generating",
            step_id=step_id_generating,
        )
        yield emitter.status("generating", "生成详细内容...", phase="generating")

        sections: list[ContentSection] = []
        full_content = ""

        # Start text message
        message_id = str(uuid.uuid4())
        yield emitter.text_message_start(message_id=message_id)

        for i, section_title in enumerate(outline.sections):
            # Emit section header
            section_header = f"\n\n# {section_title}\n\n"
            yield emitter.text_message_content(section_header)
            full_content += section_header

            section_content = ""
            async for chunk in self._generate_section(
                section_title=section_title,
                outline=outline,
                doc_type=doc_type,
                context=context,
                previous_sections=sections,
            ):
                section_content += chunk
                yield emitter.text_message_content(chunk)

            sections.append(
                ContentSection(
                    title=section_title,
                    content=section_content,
                )
            )
            full_content += section_content

            # Emit progress status
            progress = (i + 1) / len(outline.sections)
            yield emitter.status(
                "generating",
                f"正在生成: {section_title}",
                phase="generating",
                progress=progress,
            )

        yield emitter.text_message_end()
        yield emitter.step_finished(step_id=step_id_generating)

        # Step 4: Validation
        step_id_validating = str(uuid.uuid4())
        yield emitter.step_started(
            step_name="验证内容质量",
            step_type="validating",
            step_id=step_id_validating,
        )
        yield emitter.status("validating", "验证内容质量...", phase="validating")

        validation = self.guardrails.validate(full_content, doc_type)

        # Emit state snapshot with validation results
        yield emitter.state_snapshot(
            {
                "validation": {
                    "passed": validation.passed,
                    "score": validation.score,
                    "issues": [i.to_dict() for i in validation.issues],
                },
            }
        )

        # Step 5: Self-repair if needed
        repair_attempts = 0
        while not validation.passed and repair_attempts < self.MAX_REPAIR_ATTEMPTS:
            repair_attempts += 1

            step_id_repair = str(uuid.uuid4())
            yield emitter.step_started(
                step_name=f"修复质量问题 ({repair_attempts}/{self.MAX_REPAIR_ATTEMPTS})",
                step_type="repairing",
                step_id=step_id_repair,
            )
            yield emitter.status(
                "repairing",
                f"检测到质量问题，正在修复 ({repair_attempts}/{self.MAX_REPAIR_ATTEMPTS})...",
                phase="repairing",
            )

            # Agent self-repair
            repaired_content = ""
            repair_message_id = str(uuid.uuid4())
            yield emitter.text_message_start(message_id=repair_message_id, role="assistant")

            async for chunk in self._repair_content(
                content=full_content,
                issues=validation.issues,
                doc_type=doc_type,
            ):
                repaired_content += chunk
                yield emitter.text_message_content(chunk)

            yield emitter.text_message_end()

            full_content = repaired_content

            # Re-validate
            validation = self.guardrails.validate(full_content, doc_type)

            # Emit updated state
            yield emitter.state_delta(
                [
                    {"op": "replace", "path": "/validation/passed", "value": validation.passed},
                    {"op": "replace", "path": "/validation/score", "value": validation.score},
                    {
                        "op": "replace",
                        "path": "/validation/issues",
                        "value": [i.to_dict() for i in validation.issues],
                    },
                ]
            )

            yield emitter.step_finished(step_id=step_id_repair)

        yield emitter.step_finished(step_id=step_id_validating)

        # Emit completion
        yield emitter.state_snapshot(
            {
                "content": full_content,
                "word_count": self._count_words(full_content),
                "validation_passed": validation.passed,
                "repair_attempts": repair_attempts,
                "outline": {
                    "title": outline.title,
                    "sections": outline.sections,
                },
            }
        )

        yield emitter.run_finished(
            metadata={
                "word_count": self._count_words(full_content),
                "section_count": len(sections),
                "validation_passed": validation.passed,
                "repair_attempts": repair_attempts,
            }
        )

        yield emitter.stream_end()

    async def _generate_outline(
        self,
        request: str,
        doc_type: DocumentType,
        context: dict[str, Any],
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

        prompt = f"""你是一位专业的文档撰写专家。请为以下请求创建一份详细、专业的内容大纲：

用户请求：{request}
文档类型：{doc_type.value}

大纲要求：
- 至少 {min_sections} 个章节，涵盖主题的各个重要方面
- 每个章节标题具体明确，避免过于笼统
- 结构层次分明，逻辑递进
- 包含引言/背景、核心内容、分析/讨论、结论等关键部分
- 考虑实际应用场景和读者需求

输出JSON格式：
```json
{{
    "title": "专业且具体的文档标题",
    "sections": ["引言/背景", "核心概念", "详细分析", "案例研究", "实践应用", "总结与展望"]
}}
```"""

        response = await self._call_llm(prompt)

        try:
            # Extract JSON from response
            import re

            json_match = re.search(r"```json\s*([\s\S]*?)\s*```", response)
            data = json.loads(json_match.group(1)) if json_match else json.loads(response)

            return ContentOutline(
                title=data.get("title", request[:50]),
                sections=data.get("sections", [f"章节 {i + 1}" for i in range(min_sections)]),
            )
        except (json.JSONDecodeError, KeyError):
            # Fallback to default structure
            return ContentOutline(
                title=request[:50],
                sections=[f"章节 {i + 1}" for i in range(min_sections)],
            )

    async def _generate_section(
        self,
        section_title: str,
        outline: ContentOutline,
        doc_type: DocumentType,
        context: dict[str, Any],
        previous_sections: list[ContentSection],
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
            prev_context = "已完成章节：\n" + "\n".join(f"- {s.title}" for s in previous_sections)

        prompt = f"""你是一位资深专家，正在撰写专业文档《{outline.title}》。请为 "{section_title}" 章节撰写深度、详尽的内容。

文档完整大纲：
{chr(10).join(f"- {s}" for s in outline.sections)}

{prev_context}

撰写要求（严格遵守）：
1. 内容详实充分：至少 {min_words_per_section} 字，这是最低要求
2. 深度分析：
   - 提供详细的解释和论述
   - 包含具体的数据、事实和案例
   - 从多个角度深入分析问题
3. 结构清晰：
   - 使用 ## 二级标题来组织子主题
   - 适当使用项目符号列表
   - 段落之间逻辑连贯
4. 专业性：
   - 使用准确的专业术语
   - 引用相关理论或框架
   - 体现专业深度
5. 绝对禁止：
   - 禁止使用"等等"、"诸如此类"、"等内容"等模糊表达
   - 禁止使用省略号代替内容
   - 禁止使用空泛的描述，一切内容都要具体

直接输出章节内容，不要写章节标题。用实质性内容填充每一段。"""

        async for chunk in self._stream_llm(prompt):
            yield chunk

    async def _repair_content(
        self,
        content: str,
        issues: list[QualityIssue],
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
            f"- [{i.severity.value}] {i.message} (建议: {i.action})" for i in issues
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
        if hasattr(self.llm_client, "messages"):
            # Anthropic
            response = await self.llm_client.messages.create(
                model=self.model_name,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        if hasattr(self.llm_client, "chat"):
            # OpenAI-compatible
            response = await self.llm_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
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
        if hasattr(self.llm_client, "messages"):
            # Anthropic streaming
            async with self.llm_client.messages.stream(
                model=self.model_name,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text

        elif hasattr(self.llm_client, "chat"):
            # OpenAI-compatible streaming
            stream = await self.llm_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
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

        clean = re.sub(r"[#*`~\[\](){}|]", "", content)
        clean = re.sub(r"\s+", " ", clean).strip()
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", clean))
        english_words = len(re.findall(r"[a-zA-Z]+", clean))
        return chinese_chars + english_words


def create_content_generator(
    llm_client: Any,
    guardrails: QualityGuardrails | None = None,
    model_name: str = "qwen3.6-plus",
) -> DeepContentGenerator:
    """
    Factory function to create a DeepContentGenerator.

    Args:
        llm_client: LLM client
        guardrails: Quality guardrails
        model_name: Model to use (default: claude-sonnet-4 for quality)

    Returns:
        Configured DeepContentGenerator
    """
    return DeepContentGenerator(
        llm_client=llm_client,
        guardrails=guardrails,
        model_name=model_name,
    )
