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


def _coerce_questions(raw: Any) -> list[dict[str, Any]]:
    """Normalise the ``questions`` arg passed by the model to ``generate_quiz``.

    Mirror of ``_coerce_slides`` in ``agent_loop.py``. Models (Qwen 3.6 in
    particular) sometimes mis-shape this arg the same three ways that
    crashed the PPTX outline emitter on 2026-04-28:

      * Whole arg as a JSON-encoded string
        (``questions='[{"question_text": ...}]'``).
      * Items as plain strings (``questions=["What is X?", ...]``) —
        pre-bullet model output before the structured tool-call shape
        is finalised.
      * Mixed list (some dicts, some strings).

    Anything else (None, int, dict-at-top, etc.) → empty list. The
    executor itself still rejects empty lists with "No questions
    provided", so the user-visible failure mode is unchanged; this
    helper just ensures we never AttributeError inside ``q.get(...)``.
    """
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        raw = parsed

    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # Lift bare-string items into the minimal dict shape the
            # downstream validator expects. Use the string as the
            # question text (truncated to 80 chars to mirror the slide
            # title limit) so the user sees something coherent if the
            # quiz somehow squeaks through; the validator will reject
            # it for missing options on objective types but that's a
            # cleaner error than ``'str' object has no attribute 'get'``.
            out.append(
                {
                    "question_text": item[:80] or f"Question {idx}",
                    "question_type": "mc_single",
                }
            )
        # else: silently skip — int / None / nested-list have no sane
        # interpretation and would surprise the executor.
    return out


QUIZ_GENERATION_DEFINITION = ToolDefinition(
    name="generate_quiz",
    description=(
        "Create an interactive quiz from content already available in the conversation or from "
        "relevant retrieved knowledge. Supply the complete questions array. After a successful "
        "call, reply with one brief confirmation because the UI renders the questions and answers."
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
                "Array of question objects. Use EXACTLY the field names below — "
                'never "answer" (use "correct_answer") or "question" (use "question_text"). '
                'For mc_single: correct_answer is a list with ONE letter, e.g. ["B"]. '
                'For mc_multi: 2-3 letters, e.g. ["A","C"]. '
                'For true_false: ["true"] or ["false"]. '
                "option.text must be the actual answer text, never the letter itself."
            ),
            required=True,
            # Full nested JSON Schema. Gemini / Qwen / Claude structured
            # output modes enforce this at the provider side, so the model
            # cannot emit `answer` instead of `correct_answer` or forget
            # `question_text` — the retry-until-schema-right loop that
            # produced three-pills-for-one-logical-call symptoms goes away.
            items={
                "type": "object",
                "properties": {
                    "question_num": {
                        "type": "integer",
                        "description": "1-based position in the quiz",
                    },
                    "question_type": {
                        "type": "string",
                        "enum": ["mc_single", "mc_multi", "true_false"],
                        "description": "mc_single = one correct letter; "
                        "mc_multi = 2-3 correct letters; true_false = "
                        "['true'] or ['false'].",
                    },
                    "question_text": {
                        "type": "string",
                        "description": "The question itself, as a sentence. "
                        "Never put the whole thing into options.",
                    },
                    "options": {
                        "type": "array",
                        "description": "Exactly 4 options for mc_single / "
                        "mc_multi. Omit or empty for true_false.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "enum": ["A", "B", "C", "D"],
                                    "description": "The option letter.",
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Actual answer text — "
                                    "never just 'A'/'B'/'C'/'D'.",
                                },
                            },
                            "required": ["label", "text"],
                        },
                    },
                    "correct_answer": {
                        "type": "array",
                        "description": "Letters of the correct option(s): "
                        '["B"] for mc_single, ["A","C"] for mc_multi, '
                        '["true"] / ["false"] for true_false. Never prose, '
                        "never the option text, never numeric indices.",
                        "items": {"type": "string"},
                    },
                    "explanation": {
                        "type": "string",
                        "description": "One sentence on why the answer is correct.",
                    },
                },
                "required": [
                    "question_num",
                    "question_type",
                    "question_text",
                    "correct_answer",
                ],
            },
        ),
    ],
    category=ToolCategory.GENERATION,
    risk_level=ToolRiskLevel.LOW,
    capability_metadata={
        "operation_kind": "write",
        "external_service": True,
    },
    when_to_use=(
        "Use when the user explicitly asks for a quiz, test, flashcards, or practice questions. "
        "Retrieve knowledge first only when the requested quiz depends on available enterprise data; "
        "use uploaded content directly when that is the requested source."
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
        # Defend against payload mis-shaping (JSON-encoded string, list
        # of bare strings, mixed list). Mirror of ``_coerce_slides`` —
        # see helper docstring for the prod incident corpus.
        questions = _coerce_questions(args.get("questions"))

        if not questions:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="No questions provided. Generate questions from KB/file content and pass them as the 'questions' parameter.",
            )

        # Hard cap on questions per call — abuse guard (DB bloat, storage)
        _MAX_QUESTIONS_PER_QUIZ = 50
        if len(questions) > _MAX_QUESTIONS_PER_QUIZ:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Too many questions ({len(questions)}). Maximum is {_MAX_QUESTIONS_PER_QUIZ} per quiz.",
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
                            normalized_opts.append(
                                {"label": str(opt["label"]), "text": str(opt["text"])}
                            )
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
                            normalized_opts.append(
                                {"label": m.group(1).upper(), "text": m.group(2)}
                            )
                        else:
                            normalized_opts.append(
                                {"label": chr(65 + len(normalized_opts)), "text": opt}
                            )
                q["options"] = normalized_opts

                # Validate: reject the specific bug signature where text == label
                # (the LLM filled both fields with "A"/"B"/"C"/"D" instead of actual answer text)
                bad_options = [
                    o
                    for o in normalized_opts
                    if o["text"].strip().upper() == o["label"].strip().upper()
                    and len(o["text"].strip()) <= 2  # single-letter text matching label
                ]
                if bad_options and len(bad_options) == len(normalized_opts):
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=(
                            f"Question {i + 1} has invalid options: each option's 'text' field "
                            f"must contain the actual answer text, not the letter label. "
                            f"Got all options where text equals label (e.g. {{'label':'A','text':'A'}}). "
                            f"Retry with proper option text."
                        ),
                    )

                # Normalize correct_answer: convert text values to labels.
                # LLM may return ["2.5%"] instead of ["C"] — map text back to label.
                #
                # CRITICAL (regression fix): every branch below MUST either
                # produce a valid label from label_set / {"true","false"} or
                # return an error. Silent fallback on empty/garbage values
                # produced the "all-A uniform quiz" bug: an empty string
                # satisfied `"" in <any option text>` and collapsed every
                # question onto the first option's label (A). The tool now
                # rejects with a clear error so the LLM can retry properly.
                q_type = q.get("question_type", "mc_single")
                raw_answers = q.get("correct_answer") or []

                if q_type != "true_false" and not normalized_opts:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=f"Question {i + 1}: options are required for {q_type} questions.",
                    )

                # For true_false, if the LLM omitted options (observed on
                # Gemini 3.1 Pro via Vertex — the model treats the
                # statement itself as the question and considers
                # True/False implicit) synthesize the canonical
                # ``[{true, True}, {false, False}]`` pair so the UI
                # renders clickable buttons and the grader can label-match.
                # The label_set / text_to_label / idx maps a few lines
                # below will pick up these synthesized entries.
                if q_type == "true_false" and not normalized_opts:
                    normalized_opts = [
                        {"label": "true", "text": "True"},
                        {"label": "false", "text": "False"},
                    ]
                    q["options"] = normalized_opts

                label_set = {o["label"].upper() for o in normalized_opts}
                text_to_label = {
                    opt["text"].strip().lower(): opt["label"] for opt in normalized_opts
                }
                idx0_to_label = {str(k): opt["label"] for k, opt in enumerate(normalized_opts)}
                idx1_to_label = {str(k + 1): opt["label"] for k, opt in enumerate(normalized_opts)}
                explanation = q.get("explanation", "").lower()

                normalized_answers: list[str] = []
                for ans in raw_answers:
                    ans_str = str(ans).strip() if ans is not None else ""
                    ans_lower = ans_str.lower()

                    # Empty / None / whitespace-only → reject immediately.
                    # The prior code fell through to `"" in text` which matched
                    # every option and collapsed all questions onto label A.
                    if not ans_str:
                        return ToolCallResult(
                            call_id=request.call_id,
                            tool_name=request.tool_name,
                            success=False,
                            error=(
                                f"Question {i + 1}: correct_answer contains an empty value. "
                                f"Each entry must be a valid option label (e.g. 'B'). "
                                f"Retry with the correct letter for each question."
                            ),
                        )

                    # Valid label (A/B/C/D) or true/false?
                    if q_type == "true_false" and ans_lower in ("true", "false"):
                        normalized_answers.append(ans_lower)
                        continue
                    if ans_str.upper() in label_set:
                        normalized_answers.append(ans_str.upper())
                        continue

                    # Numeric index — LLMs may return 0- or 1-indexed.
                    if ans_str in idx0_to_label:
                        label_0 = idx0_to_label[ans_str]
                        if ans_str in idx1_to_label:
                            label_1 = idx1_to_label[ans_str]
                            opt_0_text = next(
                                (
                                    o["text"].lower()
                                    for o in normalized_opts
                                    if o["label"] == label_0
                                ),
                                "",
                            )
                            opt_1_text = next(
                                (
                                    o["text"].lower()
                                    for o in normalized_opts
                                    if o["label"] == label_1
                                ),
                                "",
                            )
                            if (
                                explanation
                                and opt_0_text
                                and any(w in explanation for w in opt_0_text.split()[:3])
                            ):
                                normalized_answers.append(label_0)
                            elif (
                                explanation
                                and opt_1_text
                                and any(w in explanation for w in opt_1_text.split()[:3])
                            ):
                                normalized_answers.append(label_1)
                            else:
                                normalized_answers.append(label_0)
                        else:
                            normalized_answers.append(label_0)
                        continue
                    if ans_str in idx1_to_label:
                        normalized_answers.append(idx1_to_label[ans_str])
                        continue

                    # Exact text match.
                    if ans_lower in text_to_label:
                        normalized_answers.append(text_to_label[ans_lower])
                        continue

                    # Substring match — guarded. Require needle length >=3 so
                    # an empty / single-char answer can never match every
                    # option, AND require unambiguous match (exactly one hit)
                    # so "a" doesn't match 3 options and silently pick one.
                    if len(ans_lower) >= 3:
                        matches = [
                            label
                            for text, label in text_to_label.items()
                            if text and (ans_lower in text or text in ans_lower)
                        ]
                        if len(matches) == 1:
                            normalized_answers.append(matches[0])
                            continue

                    # Prose with a leading label — "B) Self-attention",
                    # "B. ...", "B: ...", and CJK variants "B、..."
                    # "B：..." — salvage the letter.
                    if (
                        len(ans_str) >= 2
                        and ans_str[0].upper() in label_set
                        and not ans_str[1].isalnum()
                    ):
                        normalized_answers.append(ans_str[0].upper())
                        continue

                    # Nothing matched → reject. Don't store garbage that
                    # silently mis-grades.
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=(
                            f"Question {i + 1}: correct_answer {ans_str!r} does not "
                            f"match any option label {sorted(label_set)}. "
                            f"Use the letter (e.g. 'B') that matches one of "
                            f"the options you provided."
                        ),
                    )

                if not normalized_answers:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=(
                            f"Question {i + 1}: correct_answer is empty. "
                            f"Provide the letter of the correct option (e.g. ['B'])."
                        ),
                    )

                # For mc_single the grader only scores against the first
                # label, but the frontend highlights EVERY label in
                # correct_answer as "correct". If the LLM emitted 2+
                # labels (a known failure mode), the user sees multiple
                # green options while the backend silently grades only
                # the first — classic "why is my correct answer wrong?"
                # bug. Collapse to exactly one label for mc_single so the
                # UI and grader always agree.
                #
                # NOTE: this squash is NOT the root cause of the all-A bug
                # we just fixed above — it only fires when 2+ labels are
                # already present, and the symptom case started with only
                # ["A"] / [""] / [] so this branch never ran. The squash
                # remains intentional for the separate "ghost-green-wrong"
                # mc_single failure mode it was introduced for.
                if q_type == "mc_single" and len(normalized_answers) > 1:
                    logger.warning(
                        f"Question {i + 1}: mc_single received "
                        f"{len(normalized_answers)} correct answers "
                        f"{normalized_answers!r}; keeping first only"
                    )
                    normalized_answers = [normalized_answers[0]]
                q["correct_answer"] = normalized_answers

            # Cross-question sanity: reject quizzes where every objective
            # question has IDENTICAL correct_answer and the batch is big
            # enough that uniformity is overwhelmingly more likely to be an
            # LLM failure than a legitimate quiz. Observed failure mode:
            # Gemini 3 emitted ["A"] for all 5 questions after a
            # retry-after-error (question → question_text arg-name fix-up).
            # The resulting quiz grades every non-A pick as wrong and is
            # useless. Catching this here forces the model to regenerate.
            # Threshold 5: at 3-4 questions a legitimately uniform quiz
            # (e.g. a Zakat quiz where B=2.5% is always the right answer for
            # a rate-themed drill) is statistically plausible enough that
            # rejection is too aggressive. At ≥5 the probability of a
            # well-designed quiz being truly uniform is near zero, so this
            # is almost always an LLM failure mode.
            _UNIFORM_MIN_QUESTIONS = 5
            objective_qs = [
                q
                for q in questions
                if q.get("question_type", "mc_single") in ("mc_single", "mc_multi", "true_false")
            ]
            if len(objective_qs) >= _UNIFORM_MIN_QUESTIONS:
                first_ans = tuple(objective_qs[0].get("correct_answer") or [])
                if first_ans and all(
                    tuple(q.get("correct_answer") or []) == first_ans for q in objective_qs
                ):
                    logger.warning(
                        "Rejecting uniform-answer quiz: %d objective questions "
                        "all have correct_answer=%r (threshold=%d, total_qs=%d)",
                        len(objective_qs),
                        list(first_ans),
                        _UNIFORM_MIN_QUESTIONS,
                        len(questions),
                    )
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=False,
                        error=(
                            f"All {len(objective_qs)} questions have the "
                            f"same correct_answer {list(first_ans)!r}. "
                            f"This is almost always an LLM failure mode, "
                            f"not a real quiz. Regenerate with varied "
                            f"correct answers distributed across A/B/C/D."
                        ),
                    )

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
                q_rows.append(
                    (
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
                    )
                )

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

            # Build a model-facing `result` that contains the actual question
            # content — NOT just a summary sentence. Previously this tool
            # returned only "Quiz 'X' created with N questions…", so a
            # follow-up turn (especially after a model switch that clears
            # provider-side state) had no visibility into the questions and
            # would hallucinate a brand-new set when asked "explain those 5
            # questions above".
            #
            # Head-cap to 20 questions to keep long quizzes from blowing up
            # the context window on the next turn. The frontend QuizCard
            # always renders the full set from `metadata.quiz_data`.
            _MODEL_HEAD_CAP = 20
            _model_qs = questions[:_MODEL_HEAD_CAP]
            _lines: list[str] = [
                f"Quiz '{title}' created (id={quiz_id}, {len(questions)} questions, "
                f"difficulty={difficulty}). The interactive card is rendered for the user.",
                "",
                "Question content (for follow-up reference — do NOT re-list to the user):",
            ]
            for q in _model_qs:
                q_num = q.get("question_num")
                q_text = str(q.get("question_text") or "").strip()
                _lines.append(f"Q{q_num}. {q_text}")
                for opt in q.get("options") or []:
                    if isinstance(opt, dict):
                        label = opt.get("label", "?")
                        text = str(opt.get("text") or "").strip()
                        _lines.append(f"  {label}) {text}")
                correct = q.get("correct_answer") or []
                if correct:
                    _lines.append(f"  correct: {', '.join(str(c) for c in correct)}")
                expl = str(q.get("explanation") or "").strip()
                if expl:
                    _lines.append(f"  explanation: {expl}")
            if len(questions) > _MODEL_HEAD_CAP:
                _lines.append(
                    f"... [{len(questions) - _MODEL_HEAD_CAP} more questions truncated; "
                    f"full set in the card]"
                )
            model_result = "\n".join(_lines)

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=model_result,
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
