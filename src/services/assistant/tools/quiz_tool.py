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
        "MUST USE this tool when user asks for quiz, test, or practice questions. "
        "Generates an interactive quiz card from knowledge base content. "
        "Do NOT generate quiz questions as plain text — always call this tool instead. "
        "The tool creates a graded, interactive quiz UI with score tracking."
    ),
    parameters=[
        ToolParameter(
            name="topic",
            type="string",
            description="The topic to generate questions about. Extract from user's message.",
            required=True,
        ),
        ToolParameter(
            name="question_count",
            type="number",
            description="Number of questions (1-10). Default 5. Extract from user's message if specified.",
            required=False,
            default=5,
        ),
        ToolParameter(
            name="question_types",
            type="array",
            description="Question types: mc_single, mc_multi, true_false, short_answer. Default mc_single.",
            required=False,
            items={"type": "string", "enum": ["mc_single", "mc_multi", "true_false", "short_answer"]},
        ),
        ToolParameter(
            name="difficulty",
            type="string",
            description="easy, medium, or hard. Default medium.",
            required=False,
            default="medium",
            enum=["easy", "medium", "hard"],
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "ALWAYS use this tool when the user's message contains ANY of these intents: "
        "quiz, test, 测验, 测试, 出题, 考考, 练习, flashcard, practice questions, "
        "test my knowledge, check my understanding, 考我, 题目. "
        "This tool is MANDATORY for quiz requests — never answer quiz requests with plain text."
    ),
    when_not_to_use=(
        "Only skip if the user is asking a factual question that expects a direct answer, "
        "NOT a quiz or test format."
    ),
    examples=[
        ToolExample(
            description="Chinese: 出5道题测试我",
            input={"topic": "Zakat", "question_count": 5, "difficulty": "medium"},
            expected_output="Interactive quiz card with 5 MC questions",
        ),
        ToolExample(
            description="English: quiz me on 3 questions about prayer",
            input={"topic": "prayer", "question_count": 3, "difficulty": "medium"},
            expected_output="Interactive quiz card with 3 questions",
        ),
        ToolExample(
            description="出3道关于Zakat的选择题",
            input={"topic": "Zakat", "question_count": 3, "question_types": ["mc_single"]},
            expected_output="3 multiple choice questions about Zakat",
        ),
    ],
    timeout_seconds=120,
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
            missing = []
            if not retriever:
                missing.append("kb_service/kb_proxy")
            if not self.model_registry:
                missing.append("model_registry")
            if not self.database:
                missing.append("database")
            logger.error(f"Quiz tool missing services: {missing}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Quiz generation services not available (missing: {', '.join(missing)})",
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
                        top_k=12, mode="hybrid",
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
