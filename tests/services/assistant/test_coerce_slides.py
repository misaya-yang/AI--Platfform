"""``_coerce_slides`` defends the generate_pptx outline emitter.

Regression: 2026-04-28 prod incident — Qwen 3.6 frequently passed the
``slides`` arg as a JSON-encoded string OR a plain list of strings,
crashing the outline emitter (`'str' object has no attribute 'get'`)
and aborting the tool call. The agent loop hit its iteration limit and
returned a half-finished narrative ("已搜集到充足资料，现在为你生成PPT。")
without ever telling the user the tool failed.
"""

from __future__ import annotations

from assistant_service.core.agent.agent_loop import _coerce_slides


class TestCoerceSlides:
    def test_list_of_dicts_passes_through(self):
        slides = [{"title": "A", "bullets": ["x"]}, {"title": "B"}]
        assert _coerce_slides(slides) == slides

    def test_list_of_strings_lifted_to_dict_shape(self):
        out = _coerce_slides(["intro", "method", "results"])
        assert len(out) == 3
        assert out[0] == {"title": "intro", "layout": "content", "bullets": []}
        assert out[1]["title"] == "method"
        assert out[2]["title"] == "results"

    def test_json_string_parsed_into_list_of_dicts(self):
        raw = '[{"title": "Hello"}, {"title": "World"}]'
        out = _coerce_slides(raw)
        assert out == [{"title": "Hello"}, {"title": "World"}]

    def test_json_string_with_strings_inside_lifted(self):
        raw = '["one", "two"]'
        out = _coerce_slides(raw)
        assert len(out) == 2
        assert out[0]["title"] == "one"
        assert out[1]["title"] == "two"

    def test_invalid_json_string_falls_back_to_empty(self):
        # Better an empty deck than a 1-slide deck whose title is the
        # entire malformed string.
        assert _coerce_slides("not json [") == []

    def test_none_returns_empty_list(self):
        assert _coerce_slides(None) == []

    def test_dict_at_top_level_returns_empty(self):
        # A bare dict isn't a list of slides; resist the urge to lift
        # it into ``[dict]`` because the tool would happily produce
        # a 1-slide deck with surprising fields.
        assert _coerce_slides({"title": "lone slide"}) == []

    def test_mixed_list_keeps_dicts_lifts_strings_skips_garbage(self):
        out = _coerce_slides(
            [{"title": "real"}, "stringy", 42, None, {"title": "another"}]
        )
        assert len(out) == 3
        assert out[0] == {"title": "real"}
        assert out[1]["title"] == "stringy"
        assert out[2] == {"title": "another"}

    def test_long_string_title_is_truncated_to_80_chars(self):
        long = "x" * 200
        out = _coerce_slides([long])
        assert len(out[0]["title"]) == 80
        assert out[0]["title"] == "x" * 80

    def test_empty_string_item_falls_back_to_slide_n(self):
        out = _coerce_slides(["", ""])
        assert out[0]["title"] == "Slide 1"
        assert out[1]["title"] == "Slide 2"
