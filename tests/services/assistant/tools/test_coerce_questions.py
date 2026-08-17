"""``_coerce_questions`` defends the generate_quiz executor.

Regression class: 2026-04-28 prod incident — Qwen 3.6 frequently passed
structured tool args as a JSON-encoded string OR a plain list of strings,
crashing the PPTX outline emitter (``'str' object has no attribute 'get'``).
The same payload mis-shape reaches ``generate_quiz`` whose executor walks
``questions`` calling ``q.get(...)``. This helper mirrors
``_coerce_slides`` and normalises the payload at ingress.
"""

from __future__ import annotations

import copy

from assistant_service.core.tools.quiz_tool import _coerce_questions


class TestCoerceQuestions:
    def test_list_of_dicts_passes_through(self):
        questions = [
            {"question_text": "What is 2+2?", "question_type": "mc_single"},
            {"question_text": "Is the sky blue?", "question_type": "true_false"},
        ]
        assert _coerce_questions(questions) == questions

    def test_list_of_dicts_is_detached_from_caller_owned_arguments(self):
        questions = [
            {
                "question_text": "What is 2+2?",
                "question_type": "mc_single",
                "options": [{"label": "A", "text": "4"}],
            }
        ]
        original = copy.deepcopy(questions)

        out = _coerce_questions(questions)
        out[0]["question_num"] = 1
        out[0]["options"][0]["text"] = "mutated"

        assert questions == original

    def test_list_of_strings_lifted_to_dict_shape(self):
        out = _coerce_questions(["What is X?", "Define Y", "Compare Z"])
        assert len(out) == 3
        assert out[0] == {
            "question_text": "What is X?",
            "question_type": "mc_single",
        }
        assert out[1]["question_text"] == "Define Y"
        assert out[2]["question_text"] == "Compare Z"

    def test_json_string_parsed_into_list_of_dicts(self):
        raw = '[{"question_text": "Hello"}, {"question_text": "World"}]'
        out = _coerce_questions(raw)
        assert out == [
            {"question_text": "Hello"},
            {"question_text": "World"},
        ]

    def test_json_string_with_strings_inside_lifted(self):
        raw = '["one", "two"]'
        out = _coerce_questions(raw)
        assert len(out) == 2
        assert out[0]["question_text"] == "one"
        assert out[1]["question_text"] == "two"

    def test_invalid_json_string_falls_back_to_empty(self):
        # Better an empty quiz (which the executor cleanly rejects with
        # "No questions provided") than a 1-question quiz whose text is
        # the entire malformed string.
        assert _coerce_questions("not json [") == []

    def test_none_returns_empty_list(self):
        assert _coerce_questions(None) == []

    def test_dict_at_top_level_returns_empty(self):
        # A bare dict isn't a list of questions; resist the urge to lift
        # it into ``[dict]``.
        assert _coerce_questions({"question_text": "lone q"}) == []

    def test_mixed_list_keeps_dicts_lifts_strings_skips_garbage(self):
        out = _coerce_questions(
            [
                {"question_text": "real"},
                "stringy",
                42,
                None,
                {"question_text": "another"},
            ]
        )
        assert len(out) == 3
        assert out[0] == {"question_text": "real"}
        assert out[1]["question_text"] == "stringy"
        assert out[2] == {"question_text": "another"}

    def test_long_string_is_truncated_to_80_chars(self):
        long = "x" * 200
        out = _coerce_questions([long])
        assert len(out[0]["question_text"]) == 80
        assert out[0]["question_text"] == "x" * 80

    def test_empty_string_item_falls_back_to_question_n(self):
        out = _coerce_questions(["", ""])
        assert out[0]["question_text"] == "Question 1"
        assert out[1]["question_text"] == "Question 2"
