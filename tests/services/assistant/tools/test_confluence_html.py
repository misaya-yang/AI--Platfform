"""
Tests for Confluence HTML → structured-text conversion.

These guard the Confluence tool's biggest failure mode: a list page
(schedule, roster, release notes) being collapsed into prose so the model
sees only the first few entries. The whole point of the structured
converter is that bullets and links survive truncation.
"""

from __future__ import annotations


def test_list_items_become_markdown_bullets():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = "<ul><li>alpha</li><li>beta</li><li>gamma</li></ul>"
    out = _html_to_structured_text(html)
    assert out.count("- alpha") == 1
    assert out.count("- beta") == 1
    assert out.count("- gamma") == 1


def test_links_preserved_as_markdown():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = '<p>See <a href="https://example.com/x">the page</a> for details.</p>'
    out = _html_to_structured_text(html)
    assert "[the page](https://example.com/x)" in out


def test_headings_become_markdown_headings():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = "<h2>First Round</h2><ul><li>item1</li></ul><h2>Second Round</h2>"
    out = _html_to_structured_text(html)
    assert "## First Round" in out
    assert "## Second Round" in out


def test_excerpt_truncates_on_line_boundary():
    """A 800-char cap shouldn't chop a bullet mid-word — prefer the last
    newline before the cap so the excerpt ends cleanly."""
    from src.services.assistant.tools.confluence_tool import _excerpt_from_html

    items = "\n".join(f"<li>item {i} with some content</li>" for i in range(50))
    html = f"<ul>{items}</ul>"
    excerpt = _excerpt_from_html(html, max_chars=300)
    assert len(excerpt) > 200
    # Last non-truncation-marker line should be a complete bullet, not mid-item.
    body = excerpt.split("…[truncated")[0].rstrip()
    last_line = body.rsplit("\n", 1)[-1]
    assert last_line.startswith("- item ")


def test_excerpt_signals_truncation():
    """When truncated, the excerpt MUST say so — the model uses this signal
    to decide whether to escalate to read_confluence_page."""
    from src.services.assistant.tools.confluence_tool import _excerpt_from_html

    html = "<p>" + ("x" * 5000) + "</p>"
    excerpt = _excerpt_from_html(html, max_chars=300)
    assert "truncated" in excerpt
    assert "read_confluence_page" in excerpt


def test_full_content_under_cap_untouched():
    from src.services.assistant.tools.confluence_tool import _excerpt_from_html

    html = "<p>short page</p>"
    excerpt = _excerpt_from_html(html, max_chars=800)
    assert excerpt == "short page"  # no truncation marker, no extra whitespace
    assert "truncated" not in excerpt


def test_html_entities_decoded():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = "<p>A &amp; B &lt; C, see &quot;docs&quot;.</p>"
    out = _html_to_structured_text(html)
    assert "A & B < C" in out
    assert '"docs"' in out


def test_br_and_p_become_line_breaks():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = "<p>line one</p><p>line two</p>line three<br/>line four"
    out = _html_to_structured_text(html)
    # At least three line breaks should survive
    assert out.count("\n") >= 3


def test_nested_tags_stripped_but_text_kept():
    from src.services.assistant.tools.confluence_tool import _html_to_structured_text

    html = "<div><span><strong>bold</strong> and <em>italic</em></span></div>"
    out = _html_to_structured_text(html)
    assert "bold" in out
    assert "italic" in out
    assert "<" not in out
