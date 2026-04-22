"""
Unit tests for style_presets module.

Covers: enum completeness, modifier composition, legacy string tolerance,
DashScope tag/negative-prompt resolution, and idempotency of multi-turn
prompt composition.
"""

from __future__ import annotations

import pytest

from assistant_service.core.tools.style_presets import (
    STYLE_PRESETS,
    StyleDefinition,
    StylePreset,
    compose_styled_prompt,
    needs_prompt_injection_for_dashscope,
    resolve_dashscope_style_tag,
    resolve_negative_prompt,
    resolve_style_preset,
)


# =============================================================================
# Enum & table completeness
# =============================================================================


class TestStylePreset:
    def test_nine_user_styles_plus_default(self):
        """Contract: 9 user-facing styles + DEFAULT, no more no less."""
        assert len(StylePreset) == 10

    def test_all_expected_presets_present(self):
        expected = {
            "default", "realistic", "anime", "abstract", "oil_paint",
            "watercolor", "3d_render", "pixel_art", "sketch", "comic",
        }
        assert {p.value for p in StylePreset} == expected

    def test_every_preset_has_a_definition(self):
        for preset in StylePreset:
            assert preset in STYLE_PRESETS, f"Missing definition for {preset}"

    def test_definition_shape(self):
        for preset, definition in STYLE_PRESETS.items():
            assert isinstance(definition, StyleDefinition)
            # DEFAULT is allowed to have an empty modifier; others must not.
            if preset is StylePreset.DEFAULT:
                assert definition.prompt_modifier == ""
                assert definition.dashscope_tag == "<auto>"
            else:
                assert definition.prompt_modifier, f"{preset} has empty modifier"
                assert definition.dashscope_tag.startswith("<"), f"{preset} has malformed tag"


# =============================================================================
# resolve_style_preset — legacy tolerance
# =============================================================================


class TestResolveStylePreset:
    @pytest.mark.parametrize("raw,expected", [
        (None, StylePreset.DEFAULT),
        ("", StylePreset.DEFAULT),
        ("   ", StylePreset.DEFAULT),
        ("default", StylePreset.DEFAULT),
        ("realistic", StylePreset.REALISTIC),
        ("anime", StylePreset.ANIME),
        ("abstract", StylePreset.ABSTRACT),
        ("oil_paint", StylePreset.OIL_PAINT),
        ("watercolor", StylePreset.WATERCOLOR),
        ("3d_render", StylePreset.RENDER_3D),
        ("pixel_art", StylePreset.PIXEL_ART),
        ("sketch", StylePreset.SKETCH),
        ("comic", StylePreset.COMIC),
    ])
    def test_exact_enum_values(self, raw, expected):
        assert resolve_style_preset(raw) is expected

    @pytest.mark.parametrize("raw,expected", [
        # Frontend pre-refactor friendly names
        ("photography", StylePreset.REALISTIC),
        ("portrait", StylePreset.REALISTIC),
        ("3d", StylePreset.RENDER_3D),
        ("oil", StylePreset.OIL_PAINT),
        ("flat", StylePreset.DEFAULT),
        ("auto", StylePreset.DEFAULT),
        # DashScope raw tags
        ("<photography>", StylePreset.REALISTIC),
        ("<anime>", StylePreset.ANIME),
        ("<oil painting>", StylePreset.OIL_PAINT),
        ("<watercolor>", StylePreset.WATERCOLOR),
        ("<3d cartoon>", StylePreset.RENDER_3D),
        ("<sketch>", StylePreset.SKETCH),
        ("<auto>", StylePreset.DEFAULT),
        # Case tolerance on legacy aliases
        ("OIL", StylePreset.OIL_PAINT),
        ("Anime", StylePreset.ANIME),
    ])
    def test_legacy_aliases(self, raw, expected):
        assert resolve_style_preset(raw) is expected

    def test_unknown_string_falls_back_to_default(self):
        assert resolve_style_preset("quantum_holography_2099") is StylePreset.DEFAULT

    def test_enum_passthrough(self):
        assert resolve_style_preset(StylePreset.ANIME) is StylePreset.ANIME


# =============================================================================
# compose_styled_prompt
# =============================================================================


class TestComposeStyledPrompt:
    def test_default_returns_prompt_unchanged(self):
        assert compose_styled_prompt("a cat", StylePreset.DEFAULT) == "a cat"

    def test_empty_prompt_default(self):
        assert compose_styled_prompt("", StylePreset.DEFAULT) == ""

    def test_preset_appends_style_segment(self):
        result = compose_styled_prompt("a cat on a couch", StylePreset.ANIME)
        assert result.startswith("a cat on a couch")
        assert "Style:" in result
        assert "anime illustration" in result

    def test_trailing_punctuation_not_duplicated(self):
        """Prompt already ending in a period shouldn't get a second one."""
        result = compose_styled_prompt("a cat.", StylePreset.ANIME)
        assert "a cat.." not in result
        assert result.startswith("a cat. Style:")

    def test_trailing_whitespace_stripped_before_suffix(self):
        result = compose_styled_prompt("a cat   \n", StylePreset.WATERCOLOR)
        assert ".   " not in result
        assert "a cat. Style:" in result

    def test_idempotent_when_modifier_already_present(self):
        """Re-composing a turn that already has the modifier must not double-append.

        Protects multi-turn flows that loop back into ``compose_styled_prompt``.
        """
        once = compose_styled_prompt("a cat", StylePreset.OIL_PAINT)
        twice = compose_styled_prompt(once, StylePreset.OIL_PAINT)
        assert once == twice

    def test_idempotency_is_case_insensitive(self):
        """Idempotency check ignores casing when looking for the full modifier."""
        once = compose_styled_prompt("a cat", StylePreset.OIL_PAINT)
        upper = once.upper()  # full modifier present, just upper-cased
        result = compose_styled_prompt(upper, StylePreset.OIL_PAINT)
        assert result == upper, "should not re-append modifier to upper-cased copy"

    @pytest.mark.parametrize("preset", [
        StylePreset.REALISTIC, StylePreset.ANIME, StylePreset.ABSTRACT,
        StylePreset.OIL_PAINT, StylePreset.WATERCOLOR, StylePreset.RENDER_3D,
        StylePreset.PIXEL_ART, StylePreset.SKETCH, StylePreset.COMIC,
    ])
    def test_every_preset_produces_a_modifier(self, preset):
        result = compose_styled_prompt("a cat", preset)
        assert result != "a cat", f"{preset} produced no modifier"
        assert "Style:" in result


# =============================================================================
# DashScope tag & negative prompt
# =============================================================================


class TestDashScopeResolvers:
    @pytest.mark.parametrize("preset,expected_tag", [
        (StylePreset.DEFAULT, "<auto>"),
        (StylePreset.REALISTIC, "<photography>"),
        (StylePreset.ANIME, "<anime>"),
        (StylePreset.OIL_PAINT, "<oil painting>"),
        (StylePreset.WATERCOLOR, "<watercolor>"),
        (StylePreset.RENDER_3D, "<3d cartoon>"),
        (StylePreset.SKETCH, "<sketch>"),
    ])
    def test_native_tags(self, preset, expected_tag):
        assert resolve_dashscope_style_tag(preset) == expected_tag

    @pytest.mark.parametrize("preset", [
        StylePreset.ABSTRACT, StylePreset.PIXEL_ART, StylePreset.COMIC,
    ])
    def test_presets_without_native_tag_fall_back_to_auto(self, preset):
        assert resolve_dashscope_style_tag(preset) == "<auto>"

    def test_negative_prompt_for_realistic_excludes_cartoon(self):
        neg = resolve_negative_prompt(StylePreset.REALISTIC)
        assert "cartoon" in neg and "photograph" not in neg.lower().split(",")[0]

    def test_default_has_no_negative_prompt(self):
        assert resolve_negative_prompt(StylePreset.DEFAULT) == ""


class TestNeedsPromptInjectionForDashScope:
    def test_default_does_not_need_injection(self):
        assert needs_prompt_injection_for_dashscope(StylePreset.DEFAULT) is False

    def test_native_tagged_presets_do_not_need_injection(self):
        # DashScope has a real tag, so the prompt alone is sufficient.
        assert needs_prompt_injection_for_dashscope(StylePreset.ANIME) is False
        assert needs_prompt_injection_for_dashscope(StylePreset.OIL_PAINT) is False

    def test_untagged_presets_need_injection(self):
        # No native tag → we rely on the prompt-level modifier.
        assert needs_prompt_injection_for_dashscope(StylePreset.ABSTRACT) is True
        assert needs_prompt_injection_for_dashscope(StylePreset.PIXEL_ART) is True
        assert needs_prompt_injection_for_dashscope(StylePreset.COMIC) is True
