"""
Quiz Generation Tool — PPTX-pattern: main LLM generates quiz content,
tool only validates and persists. No separate LLM call.

The main LLM already has KB context (from search_knowledge_base) and/or
uploaded file content (from FileProcessor). It generates the full quiz
JSON as tool arguments. This tool just saves it to DB.

Performance: 70s → <1s (eliminates redundant LLM + KB calls).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

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

logger = logging.getLogger(__name__)

QUIZ_GENERATION_DEFINITION = ToolDefinition(
    name="generate_quiz",
    description=(
        "MUST USE this tool when user asks for a quiz, test, or practice questions. "
        "You generate the quiz content yourself based on KB search results or uploaded file content, "
        "then pass the complete questions array to this tool for interactive rendering. "
        "Do NOT output quiz questions as plain text — always use this tool to create an interactive quiz card."
    ),
    parameters=[
        ToolParameter(
            name="title",
            type="string",
            description="Quiz title, e.g. 'Quiz: Zakat Rules'",
            required=True,
        ),
        ToolParameter(
            name="description",
            type="string",
            description="One sentence quiz description",
            required=False,
        ),
        ToolParameter(
            name="difficulty",
            type="string",
            description="easy, medium, or hard",
            required=False,
            default="medium",
            enum=["easy", "medium", "hard"],
        ),
        ToolParameter(
            name="questions",
            type="array",
            description=(
                "Array of quiz question objects. Each must have: "
                "question_num (int), question_type ('mc_single'|'mc_multi'|'true_false'|'short_answer'), "
                "question_text (string), options (array of {label, text}), "
                "correct_answer (array, e.g. ['B']), explanation (string, 1-2 sentences). "
                "For mc_single: 4 options A-D, one correct. "
                "For true_false: options [{label:'true',text:'True'},{label:'false',text:'False'}]. "
                "For mc_multi: 4-5 options, 2-3 correct, e.g. correct_answer:['A','C']. "
                "For short_answer: options:[], correct_answer:['expected answer']."
            ),
            required=True,
            items={
                "type": "object",
                "properties": {
                    "question_num": {"type": "number"},
                    "question_type": {"type": "string", "enum": ["mc_single", "mc_multi", "true_false", "short_answer"]},
                    "question_text": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "object"}},
                    "correct_answer": {"type": "array"},
                    "explanation": {"type": "string"},
                },
                "required": ["question_num", "question_type", "question_text", "correct_answer"],
            },
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "ALWAYS use this tool when the user's message contains ANY of these intents: "
        "quiz, test, 测验, 测试, 出题, 考考, 练习, flashcard, practice questions, "
        "test my knowledge, check my understanding, 考我, 题目. "
        "You must FIRST search the knowledge base (if available) to get factual content, "
        "then generate questions based on that content and pass them to this tool. "
        "If the user uploaded a file, generate questions from the file content visible in the conversation."
    ),
    when_not_to_use=(
        "Only skip if the user is asking a factual question expecting a direct answer."
    ),
    examples=[
        ToolExample(
            description="3-question MC quiz about Zakat",
            input={
                "title": "Quiz: Zakat Fundamentals",
                "description": "Test your knowledge of Zakat rules",
                "difficulty": "medium",
                "questions": [
                    {
                        "question_num": 1,
                        "question_type": "mc_single",
                        "question_text": "What is the standard Zakat rate on wealth?",
                        "options": [
                            {"label": "A", "text": "1%"},
                            {"label": "B", "text": "2.5%"},
                            {"label": "C", "text": "5%"},
                            {"label": "D", "text": "10%"},
                        ],
                        "correct_answer": ["B"],
                        "explanation": "The standard rate is 2.5% of qualifying wealth held for one lunar year.",
                    },
                ],
            },
            expected_output="Interactive quiz card rendered in chat",
        ),
    ],
    timeout_seconds=30,  # Fast: no LLM call, just DB persist
)


class QuizGeneratorExecutor(ToolExecutor):
    """Validates and persists quiz data. No LLM call — main LLM provides content."""

    def __init__(self, database: Any | None = None, **kwargs: Any) -> None:
        self.database = database
        # Accept but ignore other kwargs for backward compat
        for key in ("kb_service", "model_registry", "kb_proxy"):
            setattr(self, key, kwargs.get(key))

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        args = request.arguments
        user = request.user

        title = args.get("title", "Quiz")
        description = args.get("description", "")
        difficulty = args.get("difficulty", "medium")
        questions = args.get("questions")

        if not questions or not isinstance(questions, list):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="No questions provided. Generate questions from KB/file content and pass them as the 'questions' parameter.",
            )

        if not self.database:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="Database not available",
            )

        if not user:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="User context required",
            )

        try:
            # Validate and normalize question structure
            for i, q in enumerate(questions):
                if not q.get("question_text"):
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=f"Question {i + 1} missing 'question_text'",
                    )
                q.setdefault("question_num", i + 1)
                q.setdefault("question_type", "mc_single")
                q.setdefault("options", [])
                q.setdefault("explanation", "")
                if not isinstance(q.get("correct_answer"), list):
                    q["correct_answer"] = [q.get("correct_answer", "")]

                # Normalize options to {label, text} format
                # LLMs may return: {"A": "text"} or {"label": "A", "text": "..."} or "A) text"
                normalized_opts = []
                for opt in q.get("options", []):
                    if isinstance(opt, dict):
                        if "label" in opt and "text" in opt:
                            normalized_opts.append(opt)
                        else:
                            # Handle {"A": "text"} format
                            for k, v in opt.items():
                                if k in ("type", "required"):
                                    continue
                                normalized_opts.append({"label": str(k), "text": str(v)})
                    elif isinstance(opt, str):
                        # Handle "A) text" or "A. text"
                        import re
                        m = re.match(r"^([A-Da-d])[.)]\s*(.*)", opt)
                        if m:
                            normalized_opts.append({"label": m.group(1).upper(), "text": m.group(2)})
                        else:
                            normalized_opts.append({"label": chr(65 + len(normalized_opts)), "text": opt})
                q["options"] = normalized_opts

            # Persist to DB
            quiz_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            kb_dataset_ids = request.metadata.get("kb_dataset_ids") or []

            await self.database.execute(
                """
                INSERT INTO quizzes (id, tenant_id, created_by, title, description,
                                     dataset_ids, topic, question_count, difficulty,
                                     config, status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                uuid.UUID(quiz_id),
                user.tenant_id,
                user.user_id,
                title,
                description,
                json.dumps(kb_dataset_ids),
                title,
                len(questions),
                difficulty,
                json.dumps({}),
                "ready",
                now,
                now,
            )

            q_rows = []
            for q in questions:
                q_id = str(uuid.uuid4())
                q["id"] = q_id
                q_rows.append((
                    uuid.UUID(q_id),
                    uuid.UUID(quiz_id),
                    q["question_num"],
                    q.get("question_type", "mc_single"),
                    q["question_text"],
                    json.dumps(q.get("options", [])),
                    json.dumps(q.get("correct_answer", [])),
                    q.get("explanation", ""),
                    json.dumps([]),
                    now,
                ))

            await self.database.executemany(
                """
                INSERT INTO quiz_questions (id, quiz_id, question_num, question_type,
                                            question_text, options, correct_answer,
                                            explanation, source_chunks, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                q_rows,
            )

            logger.info(f"Quiz persisted: {quiz_id} ({len(questions)} questions) in <1s")

            # Build response for frontend QuizCard
            quiz_data = {
                "quiz_id": quiz_id,
                "title": title,
                "description": description,
                "difficulty": difficulty,
                "question_count": len(questions),
                "questions": [
                    {
                        "id": q["id"],
                        "question_num": q["question_num"],
                        "question_type": q.get("question_type", "mc_single"),
                        "question_text": q["question_text"],
                        "options": q.get("options", []),
                    }
                    for q in questions
                ],
            }

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"Quiz '{title}' created with {len(questions)} questions. Interactive quiz card is now displayed.",
                metadata={"quiz_data": quiz_data},
            )

        except Exception as e:
            logger.error(f"Quiz persist failed: {e}", exc_info=True)
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=str(e),
            )


def register_quiz_tool(
    database: Any | None = None,
    **kwargs: Any,
) -> None:
    """Register the quiz generation tool in the global registry."""
    executor = QuizGeneratorExecutor(database=database, **kwargs)
    register_tool(QUIZ_GENERATION_DEFINITION, executor)
