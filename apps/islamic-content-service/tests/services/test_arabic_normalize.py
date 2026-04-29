"""Hadith Arabic-text bidi-noise normalizer.

Both data sources (sunnah.com via the AhmedElTabarani scraper, and
fawazahmed0 CDN) embed U+200F RIGHT-TO-LEFT MARK around Arabic
punctuation as typesetting hints. We strip them at insert+read time so
JSON consumers (Apifox / Java / AI models) don't see the noise.

These tests pin the contract:
  * U+200F RLM and U+200E LRM are stripped
  * U+200D ZWJ and U+200C ZWNJ are PRESERVED (they change letter shape)
  * Pure-text or empty inputs are passthrough no-ops
  * The normalizer is idempotent (re-applying does nothing)
"""

from __future__ import annotations

from islamic_content_service.repositories.hadith_repository import _normalize_arabic

# Real-world sample, copied from prod's muslim/2349a row (truncated):
# the U+200F appears right before each '.' in the original.
SAMPLE_WITH_RLM = "وقال ابن شهاب‏.‏ وحدثني سعيد‏.‏"


def test_normalize_arabic_strips_rlm():
    """U+200F RIGHT-TO-LEFT MARK must be stripped."""
    out = _normalize_arabic(SAMPLE_WITH_RLM)
    assert "‏" not in out
    # Surrounding glyphs preserved exactly
    assert out == "وقال ابن شهاب. وحدثني سعيد."


def test_normalize_arabic_strips_lrm():
    """U+200E LEFT-TO-RIGHT MARK must be stripped (1 row in prod has it)."""
    out = _normalize_arabic("hello‎world")
    assert "‎" not in out
    assert out == "helloworld"


def test_normalize_arabic_preserves_zwj_zwnj():
    """U+200D / U+200C are legitimate Arabic letter-shaping joiners.
    Stripping them would corrupt Persian / Urdu / certain Arabic glyphs.
    Pin: they MUST survive normalization."""
    text = "می‌خواهم"  # Persian "I want" with ZWNJ
    assert _normalize_arabic(text) == text
    text2 = "ا‍ب"  # ZWJ
    assert _normalize_arabic(text2) == text2


def test_normalize_arabic_passthrough_when_clean():
    """No-op fast path when input has no bidi marks."""
    text = "السلام عليكم ورحمة الله وبركاته"
    assert _normalize_arabic(text) is text  # same object — fast path


def test_normalize_arabic_handles_none_and_empty():
    assert _normalize_arabic(None) is None
    assert _normalize_arabic("") == ""


def test_normalize_arabic_is_idempotent():
    """Re-applying must not corrupt the result (used at insert AND read)."""
    once = _normalize_arabic(SAMPLE_WITH_RLM)
    twice = _normalize_arabic(once)
    assert once == twice


def test_normalize_arabic_strips_consecutive_marks():
    """Some sources stack `‏‏` around punctuation — strip all."""
    text = "اختبار‏‏.‏‏نهاية"
    out = _normalize_arabic(text)
    assert "‏" not in out
    assert out == "اختبار.نهاية"


def test_normalize_arabic_strips_advanced_bidi_and_replacement_char():
    out = _normalize_arabic("قبل‫داخل‬بعد⁧نص⁩�")
    assert out == "قبلداخلبعدنص"


def test_normalize_arabic_realistic_muslim_2349a_excerpt():
    """Reproduces the exact pattern from the screenshot the user reported.
    Note both ``[U+200F].[U+200F]`` clusters that Apifox highlighted."""
    real = (
        "بْنُ خَالِدٍ، عَنِ ابْنِ شِهَابٍ، عَنْ عُرْوَةَ، عَنْ عَائِشَةَ، أَنَّ\n"
        "وَقَالَ ابْنُ شِهَابٍ سَعِيدُ بْنُ الْمُسَيَّبِ أَخْبَرَنِي بِمِثْلِ ذَلِكَ‏.‏ "
        "وَهُوَ ابْنُ ثَلاَثٍ وَسِتِّينَ سَنَةً‏.‏"
    )
    out = _normalize_arabic(real)
    # No U+200F survives
    assert "‏" not in out
    # Periods still there
    assert out.count(".") == 2
    # Arabic content unchanged in length except for the 4 stripped marks
    assert len(out) == len(real) - 4
