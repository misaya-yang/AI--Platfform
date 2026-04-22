"""
Regression tests for the quiz tool's JSON Schema shape.

Bug fixed 2026-04-22: the `questions` parameter only declared
`items={"type": "object"}` with no nested shape. For providers that run
tool calling in structured-output mode (Gemini 2.5+/3.x, Qwen 3.x,
Claude 4), this left the model to infer field names purely from the
prose description. The observed failure loop was:

  1. model calls `generate_quiz(questions=[{question: ..., answer: ...}])`
  2. tool rejects with "missing 'question_text'"
  3. model retries with `question_text` but still `answer`
  4. tool rejects with "missing 'correct_answer'"
  5. model retries once more with the right keys

This produced three real `generate_quiz` pills in the Activity drawer
for one logical quiz (each with different args, so `(name, args)` dedup
correctly didn't collapse them — but the user still saw three "步" /
steps when they should have seen one).

Fix: declare a full nested JSON Schema for each question object. Gemini
/ Qwen / Claude structured-output modes enforce it at the provider
side, so the bad field names can't be emitted at all.

These tests pin that schema shape so a future refactor that accidentally
simplifies `items={"type": "object"}` back into the tool definition
fails loudly.
"""

from __future__ import annotations

from assistant_service.core.tools.quiz_tool import QUIZ_GENERATION_DEFINITION


def _questions_schema() -> dict:
    """Extract the `questions.items` sub-schema from the OpenAI tool schema."""
    openai_schema = QUIZ_GENERATION_DEFINITION.to_openai_schema()
    questions_param = openai_schema["function"]["parameters"]["properties"][
        "questions"
    ]
    return questions_param["items"]


def test_questions_items_is_typed_object():
    items = _questions_schema()
    assert items["type"] == "object"
    assert "properties" in items, (
        "questions.items must define `properties` — an empty-object schema "
        "leaves the model to guess field names from prose, which triggers "
        "the `answer` vs `correct_answer` retry loop."
    )


def test_required_field_names_are_pinned():
    """These exact names are what the validator / normalizer expect.
    If any one drifts, the model can emit a shape the tool will reject."""
    items = _questions_schema()
    required = set(items.get("required") or [])
    # question_num / question_type / question_text / correct_answer must
    # all be required — else the model omits them and the validator trips.
    for field in ("question_num", "question_type", "question_text", "correct_answer"):
        assert field in required, (
            f"{field!r} missing from `questions.items.required`. "
            f"Without it, the provider's structured mode won't enforce "
            f"the key and the model can emit the wrong name."
        )


def test_question_type_enum_matches_grader_support():
    """The grader only knows mc_single / mc_multi / true_false. The schema
    enum must match exactly — an open string lets the model invent types
    like `short_answer` which nothing downstream handles."""
    items = _questions_schema()
    q_type = items["properties"]["question_type"]
    assert q_type.get("type") == "string"
    assert set(q_type.get("enum") or []) == {
        "mc_single",
        "mc_multi",
        "true_false",
    }


def test_option_label_enum_is_abcd():
    """Labels MUST be A/B/C/D — the prior-round normalizer has entire
    branches predicated on this (`label_set = {'A','B','C','D'}`, index
    mapping, prose salvage). Drifting the enum would silently break them."""
    items = _questions_schema()
    option = items["properties"]["options"]["items"]
    assert option["type"] == "object"
    assert set(option["properties"]["label"].get("enum") or []) == {
        "A",
        "B",
        "C",
        "D",
    }
    assert "text" in option.get("required") or []


def test_correct_answer_is_array_of_strings():
    items = _questions_schema()
    correct = items["properties"]["correct_answer"]
    assert correct["type"] == "array"
    assert correct["items"]["type"] == "string"


def test_gemini_body_carries_nested_schema_through():
    """Gemini's functionDeclarations.parameters inherits the OpenAI
    parameters block verbatim via `_build_gemini_body`. If anyone changes
    that passthrough to strip `items` nesting, this test catches it."""
    import importlib

    mod = importlib.import_module(
        "assistant_service.core.models.model_registry"
    )
    reg = mod.ModelRegistry(use_default_models=True)
    openai_schema = QUIZ_GENERATION_DEFINITION.to_openai_schema()
    body = reg._build_google_body(
        model_id="gemini-3-flash-preview",
        messages=[mod.ChatMessage(role="user", content="make a quiz")],
        temperature=0.7,
        max_tokens=None,
        tools=[openai_schema],
        stream=True,
    )
    # Dig into the functionDeclarations schema
    fn_decls: list[dict] = []
    for entry in body.get("tools") or []:
        fn_decls.extend(entry.get("functionDeclarations") or [])
    quiz_decl = next((d for d in fn_decls if d["name"] == "generate_quiz"), None)
    assert quiz_decl is not None, "generate_quiz dropped from Gemini body"
    nested = quiz_decl["parameters"]["properties"]["questions"]["items"]
    assert nested.get("type") == "object"
    assert "question_text" in (nested.get("properties") or {}), (
        "nested question schema didn't survive OpenAI → Gemini conversion; "
        "Gemini will fall back to prose-only field inference and the "
        "answer-vs-correct_answer retry loop returns."
    )
