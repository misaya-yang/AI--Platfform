"""Confluence storage-format and result presentation helpers."""

from __future__ import annotations

import re

# ─── HTML → structured-text helpers ─────────────────────────────────
#
# The naive "strip all tags" approach collapses list pages into one run-on
# paragraph; `前 300 字` then covers 3-4 items out of a 30-item schedule
# and the model mistakenly concludes "that's all". These helpers keep the
# information density high (bullets, links, headings) while still budgeting
# the total character count.


_TAG_BULLET_OPEN = re.compile(r"<li[^>]*>", re.IGNORECASE)
_TAG_HEADING = re.compile(r"<h[1-6][^>]*>", re.IGNORECASE)
_TAG_PARAGRAPH = re.compile(r"</p\s*>|<br\s*/?>", re.IGNORECASE)
_TAG_LINK = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")
_EXCESS_NL = re.compile(r"\n{3,}")


def _html_to_structured_text(html: str) -> str:
    """Convert Confluence's body.view HTML into plain text that preserves
    list bullets, headings, line breaks, and hyperlinks.

    Conversions:
      <li>…</li>           →  "- …\n"
      <h1..6>…</h1..6>     →  "\n## …\n"
      </p>, <br>           →  "\n"
      <a href=URL>TEXT</a> →  "[TEXT](URL)"
      all other tags       →  stripped
    """
    if not html:
        return ""
    # Rewrite links first so the inner text isn't eaten by the tag stripper.
    t = _TAG_LINK.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", html)
    t = _TAG_BULLET_OPEN.sub("\n- ", t)
    t = _TAG_HEADING.sub("\n## ", t)
    t = re.sub(r"</h[1-6]\s*>", "\n", t, flags=re.IGNORECASE)
    t = _TAG_PARAGRAPH.sub("\n", t)
    t = _TAG_STRIP.sub("", t)
    # Normalize whitespace without destroying line structure.
    t = _WS.sub(" ", t)
    t = _EXCESS_NL.sub("\n\n", t)
    # HTML entity cleanup — basics only; full entity table would be overkill.
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&hellip;", "…"),
    ):
        t = t.replace(entity, char)
    return t.strip()


# ─── Markdown → Confluence Storage Format ──────────────────────────
#
# Confluence storage format is XHTML plus a handful of `<ac:…>` macros.
# We accept markdown input from callers (the common case — model-written
# content) and produce storage-format XHTML suitable for POST/PUT to the
# content endpoint. Raw HTML / XML passthrough when the input clearly
# starts as markup, so power users can supply macros directly.


def _md_escape(text: str) -> str:
    """HTML-escape text outside code blocks."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_FENCED_CODE = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


def _apply_inline_md(text: str) -> str:
    """Apply inline markdown conversions (code → links → bold → italic).
    Order matters: inline code is protected first so bold/italic inside
    backticks aren't mis-parsed."""
    # Protect inline code with a placeholder so subsequent regexes ignore it.
    segments: list[str] = []

    def _stash(match: re.Match) -> str:
        segments.append(f"<code>{_md_escape(match.group(1))}</code>")
        return f"\x00CODE{len(segments) - 1}\x00"

    t = _MD_INLINE_CODE.sub(_stash, text)
    t = _md_escape(t)
    # Restore placeholders
    for i, seg in enumerate(segments):
        t = t.replace(f"\x00CODE{i}\x00", seg)
    # Links (after escape so URL isn't double-escaped — we carefully keep it raw)
    t = _MD_LINK.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        t,
    )
    t = _MD_BOLD.sub(r"<strong>\1</strong>", t)
    t = _MD_ITALIC.sub(r"<em>\1</em>", t)
    return t


_MD_LINE_MARKER_RE = re.compile(
    r"(?m)"
    r"(^\s{0,3}#{1,6}\s+\S)"  # # heading
    r"|(^\s{0,3}[-*]\s+\S)"  # - bullet
    r"|(^\s{0,3}\d+\.\s+\S)"  # 1. ordered
    r"|(^```)"  # ``` fenced code
)
_MD_INLINE_MARKER_RE = re.compile(
    r"\*\*[^*\n]+\*\*"  # **bold**
    r"|(?<!\*)\*[^*\n]+\*(?!\*)"  # *italic*
    r"|`[^`\n]+`"  # `code`
    r"|\[[^\]]+\]\([^)\s]+\)"  # [text](url)
)
_MD_BULLET_PREFIX_RE = re.compile(r"^\s*[-*]\s+")
_MD_ORDERED_PREFIX_RE = re.compile(r"^\s*\d+\.\s+")


def _looks_like_markdown(text: str) -> bool:
    """Return True if `text` contains any markdown-only structural markers.

    We check the text TWICE: once as-is (catches raw markdown), and once
    with HTML tags stripped (catches the common model-confusion pattern
    of wrapping markdown in `<p>…</p>` tags, e.g. `<p># Heading</p>`).
    """
    if _MD_LINE_MARKER_RE.search(text) or _MD_INLINE_MARKER_RE.search(text):
        return True
    # Strip tags and re-check. This catches `<p># Heading</p>` etc. where
    # the markdown marker is at the start of a text node rather than the
    # start of a line. Tag-stripping is just for detection — the conversion
    # path below still sees the original text.
    untagged = re.sub(r"<[^>]+>", "\n", text)
    return bool(_MD_LINE_MARKER_RE.search(untagged) or _MD_INLINE_MARKER_RE.search(untagged))


def _markdown_to_storage(content: str) -> str:
    """Convert markdown-ish text to Confluence storage-format XHTML.

    Supported:
      - Fenced code blocks: ```lang\\n…\\n```  → <ac:structured-macro code>
      - Inline code: `x`                       → <code>x</code>
      - Headings: # / ## / … / ######          → <h1> … <h6>
      - Bullet lists: `- ` / `* `              → <ul><li>…</li></ul>
      - Ordered lists: `1. ` `2. ` …           → <ol><li>…</li></ol>
      - Links: [text](url)                     → <a href="url">text</a>
      - Bold (**x**), italic (*x*)             → <strong>/<em>
      - Blank-line paragraphs                  → <p>…</p>

    HTML passthrough: if the content looks like pure XHTML (starts with
    `<`, ends with `>`, AND contains no markdown-only markers) we send it
    through untouched so advanced users can supply raw Confluence macros
    / tables / whatever.

    The markdown-marker check is critical: previously any content that
    merely started with `<p>` and ended with `</p>` would passthrough,
    which meant models wrapping markdown in `<p>…</p>` tags would render
    literal `#` and `-` in Confluence. That regression was the original
    bug report — fix is the `_looks_like_markdown` gate below.
    """
    if not content:
        return "<p></p>"

    stripped = content.strip()
    if stripped.startswith("<") and stripped.endswith(">") and not _looks_like_markdown(stripped):
        # Pure storage-format HTML — caller knows what they're doing.
        return content

    # Pre-process: if the content mixes HTML structural wrappers with
    # markdown (`<p># Heading</p>` or `line A<br>line B<br># heading` —
    # common model outputs because the model thinks Confluence is "an
    # HTML system"), strip only the structural wrappers so the markdown
    # inside reaches the converter. We do NOT strip semantic tags like
    # <a>/<strong>/<em>/<code> — those represent formatting intent and
    # remain as valid XHTML that Confluence renders correctly.
    if _looks_like_markdown(stripped) and re.search(
        r"</?(?:p|div|br|span)\b", stripped, flags=re.IGNORECASE
    ):
        stripped = re.sub(
            r"</?(?:p|div|br\s*/?|span)(?:\s[^>]*)?>",
            "\n",
            stripped,
            flags=re.IGNORECASE,
        )
        # Collapse 3+ consecutive newlines that stripping may have created.
        stripped = re.sub(r"\n{3,}", "\n\n", stripped)
        content = stripped.strip()

    # 1. Extract fenced code blocks first so markdown inside them is preserved verbatim.
    code_blocks: list[tuple[str, str]] = []

    def _stash_code(m: re.Match) -> str:
        lang = m.group(1) or ""
        body = m.group(2)
        code_blocks.append((lang, body))
        return f"\x00FENCED{len(code_blocks) - 1}\x00"

    body_text = _MD_FENCED_CODE.sub(_stash_code, content)

    # 2. Split into blocks on blank lines (markdown paragraph semantics).
    blocks = re.split(r"\n\s*\n", body_text)
    out_parts: list[str] = []
    for block in blocks:
        block = block.rstrip()
        if not block.strip():
            continue
        lines = block.split("\n")

        # Fenced-code placeholder — replace and emit as a Confluence code macro.
        if len(lines) == 1 and re.fullmatch(r"\x00FENCED\d+\x00", lines[0]):
            idx = int(lines[0][7:-1])
            lang, body = code_blocks[idx]
            cdata_safe = body.replace("]]>", "]]]]><![CDATA[>")
            macro = (
                '<ac:structured-macro ac:name="code">'
                + (f'<ac:parameter ac:name="language">{lang}</ac:parameter>' if lang else "")
                + f"<ac:plain-text-body><![CDATA[{cdata_safe}]]></ac:plain-text-body>"
                + "</ac:structured-macro>"
            )
            out_parts.append(macro)
            continue

        # Heading (single line starting with # ... #)
        if len(lines) == 1:
            m = re.match(r"^(#{1,6})\s+(.*)$", lines[0])
            if m:
                level = len(m.group(1))
                out_parts.append(f"<h{level}>{_apply_inline_md(m.group(2))}</h{level}>")
                continue

        # Bulleted list — all lines start with `- ` or `* `
        if all(re.match(r"^\s*[-*]\s+", line) for line in lines):
            items = [
                f"<li>{_apply_inline_md(re.sub(_MD_BULLET_PREFIX_RE, '', line))}</li>"
                for line in lines
            ]
            out_parts.append("<ul>" + "".join(items) + "</ul>")
            continue

        # Ordered list — all lines start with `N. `
        if all(re.match(r"^\s*\d+\.\s+", line) for line in lines):
            items = [
                f"<li>{_apply_inline_md(re.sub(_MD_ORDERED_PREFIX_RE, '', line))}</li>"
                for line in lines
            ]
            out_parts.append("<ol>" + "".join(items) + "</ol>")
            continue

        # Plain paragraph — join lines with <br/>, apply inline markdown.
        inner = "<br/>".join(_apply_inline_md(line) for line in lines)
        out_parts.append(f"<p>{inner}</p>")

    if not out_parts:
        return "<p></p>"
    rendered = "".join(out_parts)
    # Merge consecutive list blocks that stripping HTML-wrappers produced as
    # separate single-item lists, e.g. `<ul><li>a</li></ul><ul><li>b</li></ul>`
    # → `<ul><li>a</li><li>b</li></ul>`. Cosmetic but matters for semantic
    # list rendering / ordered-list numbering.
    rendered = re.sub(r"</ul>\s*<ul>", "", rendered)
    rendered = re.sub(r"</ol>\s*<ol>", "", rendered)
    return rendered


# Standard HTML entities that are "already escaped" — if the user's
# replacement contains a bare `&` that IS followed by one of these, we
# leave it alone. A bare `&` followed by anything else becomes `&amp;`.
_BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _escape_for_storage(text: str) -> str:
    """Escape XHTML specials so plain-text content doesn't corrupt
    Confluence storage format. Idempotent for already-escaped input."""
    text = _BARE_AMP_RE.sub("&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text


def _filter_and_rank_spaces(spaces: list[dict], query: str) -> list[dict]:
    """Client-side rank spaces by fuzzy relevance to `query`.

    V2's /spaces endpoint doesn't take a name-search parameter, so we
    fetch a batch and rank in Python. Scoring:
      +3 exact substring match in name (case-insensitive)
      +2 substring match in key
      +1 substring match in description
      +1 every query token that appears anywhere
    """
    if not query or not spaces:
        return spaces

    q = query.strip().lower()
    # Split on whitespace and on common CJK separators.
    tokens = [t for t in re.split(r"[\s、，,/]+", q) if t]
    scored: list[tuple[int, dict]] = []
    for sp in spaces:
        name = (sp.get("name") or "").lower()
        key = (sp.get("key") or "").lower()
        desc = (sp.get("description") or "").lower()
        score = 0
        if q in name:
            score += 3
        if q in key:
            score += 2
        if q in desc:
            score += 1
        for tok in tokens:
            if tok and (tok in name or tok in key or tok in desc):
                score += 1
        if score > 0:
            scored.append((score, sp))

    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    return [sp for _, sp in scored]


def _excerpt_from_html(html: str, max_chars: int) -> str:
    """Return a structured excerpt from an HTML body, truncated on a line
    boundary when possible so bullet items aren't cut mid-way."""
    text = _html_to_structured_text(html)
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    # Prefer cutting at the last newline so we don't chop a list item mid-sentence.
    nl = head.rfind("\n")
    if nl >= max_chars // 2:
        head = head[:nl]
    return head.rstrip() + "\n…[truncated; use read_confluence_page for full content]"
