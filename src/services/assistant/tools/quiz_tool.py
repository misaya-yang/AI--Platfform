"""
Quiz Generation Tool — Allows the LLM to generate quizzes from KB content.

The model decides when to call this tool based on user intent.
No hardcoded regex patterns — the LLM handles intent detection naturally.
"""

from __future__ import annotations

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

QUIZ_GENERATION_DEFINITION = ToolDefinition(
    name="generate_quiz",
    description=(
        "Generate an interactive quiz from the knowledge base content. "
        "Creates multiple-choice, true/false, multi-select, or short-answer questions "
        "based on KB documents. The quiz is displayed as an interactive card in the chat "
        "where the user can answer questions and get scored."
    ),
    parameters=[
        ToolParameter(
            name="topic",
            type="string",
            description="The topic or subject to generate questions about. "
            "Use a broad phrase that covers the area the user wants to be tested on.",
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
            description="Types of questions to generate. Options: mc_single (multiple choice single answer), "
            "mc_multi (multiple correct answers), true_false, short_answer. Default is mc_single.",
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
            description="Language for the quiz. Use 'auto' to match the KB content language, "
            "or specify a language code like 'en', 'zh', 'ar'. Default is auto.",
            required=False,
            default="auto",
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "Use this tool when the user wants to be quizzed, tested, or practice their knowledge. "
        "Common triggers include: asking for a quiz, test, practice questions, flashcards, "
        "or any request to test their understanding of KB content. "
        "Examples: '出5道题测试我', 'quiz me', 'test my knowledge', 'practice questions about X', "
        "'考考我关于Y的知识', 'generate questions from the knowledge base'."
    ),
    when_not_to_use=(
        "Do not use when the user is simply asking a question that expects a direct answer. "
        "Only use when the user explicitly wants to be tested or quizzed."
    ),
    examples=[
        ToolExample(
            description="User wants a general quiz",
            input={"topic": "key concepts", "question_count": 5, "difficulty": "medium"},
            expected_output="Interactive quiz card with 5 MC questions",
        ),
        ToolExample(
            description="User wants hard true/false questions",
            input={"topic": "Islamic finance principles", "question_count": 3, "question_types": ["true_false"], "difficulty": "hard"},
            expected_output="3 true/false questions about Islamic finance",
        ),
        ToolExample(
            description="User wants mixed question types",
            input={"topic": "sales process", "question_count": 5, "question_types": ["mc_single", "true_false", "mc_multi"]},
            expected_output="5 questions mixing MC, TF, and multi-select",
        ),
    ],
    timeout_seconds=30,
)


class QuizGeneratorExecutor(ToolExecutor):
    """Executor for quiz generation tool — actual execution is handled in assistant_service."""

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        # This is a placeholder — actual execution happens in assistant_service.py
        # because it needs access to KB service, database, and model registry
        return ToolCallResult(
            task_id=request.task_id,
            tool=request.tool,
            result="Quiz generation is handled by the assistant service directly.",
            success=True,
        )


def register_quiz_tool() -> None:
    """Register the quiz generation tool in the global registry."""
    register_tool(QUIZ_GENERATION_DEFINITION, QuizGeneratorExecutor())
