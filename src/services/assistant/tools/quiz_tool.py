"""
Quiz Generation Tool — Allows the LLM to generate quizzes from KB content.

The model decides when to call this tool based on user intent.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

if TYPE_CHECKING:
    from ....persistence.database import DatabaseStorage
    from ....services.knowledge.knowledge_service import KnowledgeService
    from ..model_registry import ModelRegistry

logger = logging.getLogger(__name__)

QUIZ_GENERATION_DEFINITION = ToolDefinition(
    name="generate_quiz",
    description=(
        "Generate an interactive quiz from the knowledge base content. "
        "Creates multiple-choice, true/false, multi-select, or short-answer questions "
        "based on KB documents. The quiz is displayed as an interactive card in the chat."
    ),
    parameters=[
        ToolParameter(
            name="topic",
            type="string",
            description="The topic or subject to generate questions about.",
            required=True,
        ),
        ToolParameter(
            name="question_count",
            type="number",
            description="Number of questions to generate (1-10). Default is 5.",
            required=False,
            default=5,
        ),
        ToolParameter(
            name="question_types",
            type="array",
            description="Types of questions: mc_single, mc_multi, true_false, short_answer. Default is mc_single.",
            required=False,
            items={"type": "string", "enum": ["mc_single", "mc_multi", "true_false", "short_answer"]},
        ),
        ToolParameter(
            name="difficulty",
            type="string",
            description="Difficulty level: easy, medium, or hard. Default is medium.",
            required=False,
            default="medium",
            enum=["easy", "medium", "hard"],
        ),
        ToolParameter(
            name="language",
            type="string",
            description="Language for the quiz. 'auto' matches KB content language. Default is auto.",
            required=False,
            default="auto",
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "Use when the user wants to be quizzed, tested, or practice their knowledge. "
        "Examples: '出5道题测试我', 'quiz me', 'test my knowledge', 'practice questions'."
    ),
    when_not_to_use=(
        "Do not use when the user is simply asking a question expecting a direct answer."
    ),
    examples=[
        ToolExample(
            description="User wants a quiz",
            input={"topic": "key concepts", "question_count": 5, "difficulty": "medium"},
            expected_output="Interactive quiz card with 5 questions",
        ),
    ],
    timeout_seconds=60,
)


class QuizGeneratorExecutor(ToolExecutor):
    """Executes quiz generation: KB retrieval → LLM → persist → return quiz data."""

    def __init__(
        self,
        kb_service: Any | None = None,
        model_registry: ModelRegistry | None = None,
        database: DatabaseStorage | None = None,
        kb_proxy: Any | None = None,
    ) -> None:
        self.kb_service = kb_service
        self.kb_proxy = kb_proxy
        self.model_registry = model_registry
        self.database = database

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        args = request.arguments
        user = request.user
        topic = args.get("topic", "key concepts")
        count = min(10, max(1, int(args.get("question_count", 5))))
        q_types = args.get("question_types", ["mc_single"])
        difficulty = args.get("difficulty", "medium")
        language = args.get("language", "auto")

        retriever = self.kb_service or self.kb_proxy
        if not retriever or not self.model_registry or not self.database:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Quiz generation services not available",
            )

        if not user:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="User context required for quiz generation",
            )

        # Get KB dataset IDs from request metadata
        kb_dataset_ids = request.metadata.get("kb_dataset_ids") or []
        if not kb_dataset_ids:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="No knowledge base datasets selected. Please select a dataset first.",
            )

        try:
            # 1. Retrieve KB chunks
            all_chunks: list[dict[str, Any]] = []
            for ds_id in kb_dataset_ids:
                try:
                    results, _ = await retriever.retrieve(
                        user=user, dataset_id=ds_id, query=topic,
                        top_k=20, mode="hybrid",
                    )
                    for r in results:
                        all_chunks.append({
                            "content": r.text, "score": r.score,
                            "metadata": r.metadata or {},
                        })
                except Exception as e:
                    logger.warning(f"Quiz KB retrieval failed for {ds_id}: {e}")

            if not all_chunks:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error="No content retrieved from knowledge base datasets.",
                )

            # 2. Generate quiz
            from ..quiz_generator import QuizGenerator
            from ..quiz_grader import QuizGrader
            from ..quiz_service import QuizService

            generator = QuizGenerator(self.model_registry)
            grader = QuizGrader(model_registry=self.model_registry)
            svc = QuizService(db=self.database, generator=generator, grader=grader)

            quiz = await svc.create_quiz(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                dataset_ids=kb_dataset_ids,
                kb_chunks=all_chunks,
                topic=topic,
                question_count=count,
                question_types=q_types,
                difficulty=difficulty,
                language=language,
            )

            logger.info(f"Quiz tool generated: {quiz['quiz_id']} ({quiz['question_count']} questions)")

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"Quiz generated: '{quiz['title']}' with {quiz['question_count']} {difficulty} questions.",
                metadata={"quiz_data": quiz},
            )

        except Exception as e:
            logger.error(f"Quiz generation failed: {e}", exc_info=True)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )


def register_quiz_tool(
    kb_service: Any | None = None,
    model_registry: Any | None = None,
    database: Any | None = None,
    kb_proxy: Any | None = None,
) -> None:
    """Register the quiz generation tool in the global registry."""
    executor = QuizGeneratorExecutor(
        kb_service=kb_service,
        model_registry=model_registry,
        database=database,
        kb_proxy=kb_proxy,
    )
    register_tool(QUIZ_GENERATION_DEFINITION, executor)
