"""
Confluence Tool — AI can read/edit Confluence during chat.

Uses stored Confluence credentials (API Token + Email) to call
Atlassian REST API v1 (content) + v2 (spaces) directly.

Architecture: TWO meta-tools that cover 11 operations via `action` enum.
~80% smaller tool-schema catalog than 8-tool-per-operation, and avoids
the "new tool silently scores 0 in selector" pitfall.

Tools:
  - confluence_read — actions: search, read_page, list_spaces,
    get_space, list_children
  - confluence_write — actions: create_page, update_page,
    find_replace, comment, delete_page (all require confirmation)
"""

from __future__ import annotations

import re
import time
from base64 import b64encode
from typing import Any

import httpx
from ai_gateway_core.logging import get_logger

from .connector_registry import get_connector_registry
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    get_tool_registry,
)

logger = get_logger(__name__)


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


# ─── Tool Definitions ────────────────────────────────────────────────


CONFLUENCE_READ_DEFINITION = ToolDefinition(
    name="confluence_read",
    description=(
        "Read-only Confluence operations. Pick `action` from: "
        "`search` (find pages), `read_page` (fetch one page by id/title/url), "
        "`list_spaces` (find workspaces), `get_space` (one space's details), "
        "`list_children` (direct child pages of a page). "
        "Supply only the params listed per action — see `when_to_use` for "
        "full parameter semantics and failure-recovery tips."
    ),
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="Which read operation to perform. REQUIRED.",
            required=True,
            enum=["search", "read_page", "list_spaces", "get_space", "list_children"],
        ),
        # Search / list_spaces query
        ToolParameter(
            name="query",
            type="string",
            description="Keyword or phrase. For search: content full-text. For list_spaces: fuzzy match on name/key/description.",
            required=False,
        ),
        # Page identifiers (read_page / list_children)
        ToolParameter(
            name="page_id",
            type="string",
            description="Numeric page id. Required for list_children; one of page_id/title/url for read_page.",
            required=False,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Exact page title — fallback lookup for read_page when page_id is unknown.",
            required=False,
        ),
        ToolParameter(
            name="url",
            type="string",
            description="Full Confluence URL. read_page extracts page_id automatically.",
            required=False,
        ),
        # Space identifiers (get_space / search filter)
        ToolParameter(
            name="space_key",
            type="string",
            description="Space key (e.g. 'SALES'). For search: narrow to this space. For get_space: lookup.",
            required=False,
        ),
        ToolParameter(
            name="space_id",
            type="string",
            description="Numeric space id. For get_space (preferred over space_key when available).",
            required=False,
        ),
        ToolParameter(
            name="keys",
            type="array",
            description="For list_spaces: filter to these exact space keys.",
            required=False,
            items={"type": "string"},
        ),
        ToolParameter(
            name="space_type",
            type="string",
            description="For list_spaces: filter by type.",
            required=False,
            enum=["global", "personal", "collaboration", "knowledge_base"],
        ),
        ToolParameter(
            name="labels",
            type="array",
            description="For list_spaces: filter spaces that have ALL these labels.",
            required=False,
            items={"type": "string"},
        ),
        # ── Search dimensions (agentic) ──
        ToolParameter(
            name="fields",
            type="array",
            description=(
                "For search: which fields to match `query` against. Subset of "
                "['title','text']. Default both. Use ['title'] to recover when "
                "title-only pages (schedules, rosters) return 0 hits with default."
            ),
            required=False,
            items={"type": "string", "enum": ["title", "text"]},
        ),
        ToolParameter(
            name="author",
            type="string",
            description="For search: filter by creator (Atlassian accountId or username).",
            required=False,
        ),
        ToolParameter(
            name="updated_since",
            type="string",
            description="For search: ISO date 'YYYY-MM-DD' — only pages modified on/after.",
            required=False,
        ),
        ToolParameter(
            name="under_page_id",
            type="string",
            description="For search: restrict to descendants of this page (subtree scope).",
            required=False,
        ),
        ToolParameter(
            name="cql",
            type="string",
            description=(
                "For search: raw Confluence CQL. OVERRIDES every other search "
                "param. Use for advanced operators (exact title=, label=, "
                "type=blogpost, negative matches). Caller is responsible for "
                "correctness; we only reject control characters."
            ),
            required=False,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max results. Defaults vary per action (search=10, list_spaces=50, list_children=25).",
            required=False,
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    capability_metadata={
        "operation_kind": "read",
        "read_only": True,
        "external_service": True,
    },
    when_to_use=(
        "Any Confluence read task: searching pages/spaces, reading a page, "
        "navigating page tree.\n\n"
        "ACTION DETAILS:\n\n"
        "• search — find pages. Requires at least one of query, cql, "
        "author, updated_since, under_page_id. Optional dimensions:\n"
        "    - fields: subset of ['title','text']. DEFAULT both. Pages "
        "with Chinese titles often only match title (body is a table with "
        "no prose) — if default returns 0, retry fields=['title'] with a "
        "shorter, 1-3-token query.\n"
        "    - space_key: narrow to one space.\n"
        "    - author: accountId or username.\n"
        "    - updated_since: ISO date 'YYYY-MM-DD'.\n"
        "    - under_page_id: restrict to a page's subtree.\n"
        "    - cql: raw Confluence CQL, overrides everything. For exact "
        'titles (`title = "X"`), labels, blogposts, negatives.\n'
        "    - limit: default 10, max 25.\n"
        "  Returns the CQL it executed + a diagnostic hint on 0 hits. "
        "READ the hint — pick ONE narrower/broader retry, don't blindly "
        "re-phrase.\n\n"
        "• read_page — full content (up to 20K chars, markdown). Requires "
        "ONE of page_id / title / url. URL → page_id auto-extracted. "
        "Response includes parent_id + ancestor path — pass parent_id "
        "verbatim to confluence_write create_page to make a sibling.\n\n"
        "• list_spaces — find workspaces. Params: query (fuzzy on "
        "name/key/desc), keys (exact list), space_type, labels, limit "
        "(default 50). Use this BEFORE search when intent is 'find the "
        "workspace containing…'.\n\n"
        "• get_space — one space's metadata. Requires space_id or space_key.\n\n"
        "• list_children — direct child pages of a page (one level). "
        "Requires page_id. Optional limit (default 25)."
    ),
    when_not_to_use=(
        "Do not use for mutations (create/update/delete/comment) — use confluence_write instead."
    ),
    examples=[
        ToolExample(
            description="Search pages in SALES space",
            input={"action": "search", "query": "QA leads", "space_key": "SALES"},
        ),
        ToolExample(
            description="Read a page from its URL",
            input={
                "action": "read_page",
                "url": "https://x.atlassian.net/wiki/spaces/S/pages/123/Title",
            },
        ),
        ToolExample(
            description="Find a sales-related space",
            input={"action": "list_spaces", "query": "sales"},
        ),
        ToolExample(
            description="List child pages under a parent",
            input={"action": "list_children", "page_id": "1038712833"},
        ),
    ],
    relevance_keywords=[
        "confluence",
        "wiki",
        "atlassian",
        "space",
        "page",
        "workspace",
        "空间",
        "页面",
        "文档库",
        "内部文档",
        "wiki页",
        "排期",
        "search",
        "read",
        "查找",
        "搜索",
        "查看",
        "读取",
        "列出",
        "哪个空间",
        "子页面",
        "children",
    ],
    timeout_seconds=20,
)


CONFLUENCE_WRITE_DEFINITION = ToolDefinition(
    name="confluence_write",
    description=(
        "Confluence write operations. Pick `action` from: "
        "`create_page`, `update_page`, `find_replace`, `move_page`, "
        "`comment`, `delete_page`. ALL actions require user confirmation "
        "(gated by the permission layer). Content accepts markdown; "
        "converted to Confluence storage format automatically. See "
        "`when_to_use` for per-action parameters and safety rules."
    ),
    parameters=[
        ToolParameter(
            name="action",
            type="string",
            description="Which write operation to perform. REQUIRED.",
            required=True,
            enum=[
                "create_page",
                "update_page",
                "find_replace",
                "move_page",
                "comment",
                "delete_page",
            ],
        ),
        ToolParameter(
            name="page_id",
            type="string",
            description="Target page id. Required for update/find_replace/comment/delete.",
            required=False,
        ),
        ToolParameter(
            name="space_key",
            type="string",
            description="For create_page: space to create the page in.",
            required=False,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="For create_page: required. For update_page: optional rename.",
            required=False,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="For create_page / update_page: the page body (markdown or HTML).",
            required=False,
        ),
        ToolParameter(
            name="parent_id",
            type="string",
            description="For create_page: optional parent page to nest under.",
            required=False,
        ),
        ToolParameter(
            name="target_parent_id",
            type="string",
            description="For move_page: id of the page that will become the NEW parent. Must exist and be in the same space.",
            required=False,
        ),
        ToolParameter(
            name="find",
            type="string",
            description="For find_replace: exact substring to locate. Must appear exactly once.",
            required=False,
        ),
        ToolParameter(
            name="replace",
            type="string",
            description="For find_replace: substring to replace `find` with.",
            required=False,
        ),
        ToolParameter(
            name="raw_html",
            type="boolean",
            description=(
                "For find_replace: treat `replace` as raw XHTML (don't escape "
                "& / < / >). Default false — plain text is auto-escaped so "
                "innocent content like 'A & B' doesn't corrupt page markup. "
                "Only set true when intentionally inserting tags like <strong>."
            ),
            required=False,
            default=False,
        ),
        ToolParameter(
            name="body",
            type="string",
            description="For comment: the comment text.",
            required=False,
        ),
    ],
    category=ToolCategory.INTEGRATION,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=True,
    capability_metadata={
        "operation_kind": "write",
        "external_service": True,
    },
    when_to_use=(
        "User explicitly asks to create, edit, update, comment on, move, "
        "or delete a Confluence page. Always confirm destructive actions.\n\n"
        "ACTION DETAILS:\n\n"
        "• create_page — new page. Requires space_key, title, content "
        "(markdown accepted). Optional parent_id to nest under a parent. "
        "TO CREATE A SIBLING of an existing page: first read_page on the "
        "reference — use the parent_id it returns, or use parent_id from "
        "a search hit directly. Don't guess.\n\n"
        "• update_page — FULL body replacement. Requires page_id, content. "
        "Optional title (rename). ⚠️ Overwrites everything — prefer "
        "find_replace for small edits on long pages.\n\n"
        "• find_replace — targeted partial edit. Requires page_id, find, "
        "replace. Refuses if `find` has 0 or >1 matches (narrow pattern "
        "until unique). USE FOR 'change X to Y' on long pages. `replace` "
        "is auto-escaped as plain text; set raw_html=true only when "
        "intentionally inserting XHTML like <strong>.\n\n"
        "• move_page — relocate under a new parent (same space). Requires "
        "page_id, target_parent_id. Refuses cross-space moves and no-ops.\n\n"
        "• comment — add page-level comment. Requires page_id, body.\n\n"
        "• delete_page — move to trash. Requires page_id. Confirm title/id "
        "with user first.\n\n"
        "Markdown is converted to Confluence storage format automatically: "
        "headings (# — ######), bullet/ordered lists, **bold**, *italic*, "
        "`inline code`, ```fenced code```, [links](url). Raw HTML also "
        "passes through (detected and preserved)."
    ),
    when_not_to_use=(
        "Never use speculatively. Never delete pages the user did not "
        "explicitly name. For reads, use confluence_read."
    ),
    examples=[
        ToolExample(
            description="Change one line on a schedule page",
            input={
                "action": "find_replace",
                "page_id": "1038712833",
                "find": "论文分享 – Manifold-Constrained Hyper-Connections",
                "replace": "Manifold-Constrained Hyper-Connections",
            },
        ),
        ToolExample(
            description="Create a meeting-notes page",
            input={
                "action": "create_page",
                "space_key": "DEV",
                "title": "2026-04-21 Sync",
                "content": "# Decisions\n\n- Ship Phase 5\n- Freeze APIs Friday",
            },
        ),
        ToolExample(
            description="Comment on a design doc",
            input={"action": "comment", "page_id": "12345", "body": "LGTM"},
        ),
    ],
    relevance_keywords=[
        "confluence",
        "wiki",
        "atlassian",
        "page",
        "空间",
        "页面",
        "create",
        "update",
        "edit",
        "modify",
        "rewrite",
        "change",
        "delete",
        "新建",
        "创建",
        "更新",
        "编辑",
        "修改",
        "改",
        "改为",
        "改成",
        "删除",
        "comment",
        "评论",
        "反馈",
        "批注",
        "发布",
        "草稿",
    ],
    timeout_seconds=25,
)


# ─── Confluence API Client ───────────────────────────────────────────


class ConfluenceAPIClient:
    """Lightweight Confluence REST API client using stored credentials.

    Uses both API versions on purpose:
      - V1 `/wiki/rest/api/content/search` for CQL page search (stable, widely supported)
      - V2 `/wiki/api/v2/spaces` for space listing (V1 doesn't expose a GET list endpoint)
    """

    def __init__(self, domain: str, email: str, api_token: str):
        self.domain = domain
        self.base_url = f"https://{domain}/wiki/rest/api"
        self.v2_base_url = f"https://{domain}/wiki/api/v2"
        self.auth_header = "Basic " + b64encode(f"{email}:{api_token}".encode()).decode()

    async def search(
        self,
        query: str = "",
        *,
        space_key: str | None = None,
        fields: list[str] | None = None,
        author: str | None = None,
        updated_since: str | None = None,
        under_page_id: str | None = None,
        cql: str | None = None,
        limit: int = 10,
    ) -> dict:
        """Agentic Confluence search. Returns a dict with hits + the
        effective CQL + diagnostic hints so the caller can self-correct
        when a query returns nothing.

        Parameters (all optional but at least one of query/cql is required):
          - query: natural-language keywords (we build CQL from it).
          - fields: which fields to match on — any subset of
              ["title", "text"]. Default ["title", "text"].
          - space_key: restrict to a space (alnum; rejected otherwise).
          - author: restrict to a user (Atlassian accountId or username).
          - updated_since: ISO date, restricts to `lastModified >= date`.
          - under_page_id: restrict to descendants of this page (CQL
              `ancestor=<id>`).
          - cql: raw CQL. When set, overrides EVERY other parameter except
              `limit`. Use when you need operators this function doesn't
              model (text!~, type=blogpost, label=, etc.). Still escaped
              for safety: backslashes and quotes in the `cql` string are
              not touched — the caller is responsible, but we do reject
              obvious tampering like newlines.
          - limit: 1-25. Clamped.

        Returns:
          {
            "hits": [page_dict, ...],
            "count": int,
            "cql_used": "<actual CQL sent>",
            "diagnostics": {
              "strategy": "title+text" | "title" | "text" | "raw_cql",
              "brackets_stripped": bool,
              "hint": "<free-form next-step suggestion, only when 0 hits>",
            },
          }
        """
        # ---------- 1. Build CQL ----------
        used_strategy: str
        brackets_stripped = False
        if cql:
            # Reject obvious abuse; beyond that trust the caller.
            if any(c in cql for c in ("\n", "\r", "\x00")):
                raise ValueError("raw cql must be single-line, no control chars")
            effective_cql = cql
            used_strategy = "raw_cql"
        else:
            if not query and not author and not updated_since and not under_page_id:
                raise ValueError(
                    "search requires at least one of: query, author, "
                    "updated_since, under_page_id, or cql"
                )
            parts: list[str] = ["type=page"]

            # Keyword match via title/text (or either, depending on fields).
            if query:
                safe_query = query.replace("\\", "\\\\").replace('"', '\\"')
                wanted_fields = {f.lower().strip() for f in (fields or ["title", "text"])}
                # Validate field names up front.
                unknown = wanted_fields - {"title", "text"}
                if unknown:
                    raise ValueError(f"unknown fields {unknown!r}; valid: title, text")

                # CJK bracket normalization for title-only side (Lucene
                # tokenizer hates 【】). Text side keeps the raw query so
                # exact phrases with brackets still hit when they're in
                # the body.
                title_query = re.sub(r"[【】「」『』\[\]()（）]", " ", safe_query).strip()
                title_query = re.sub(r"\s{2,}", " ", title_query)
                if not title_query:
                    title_query = safe_query
                brackets_stripped = title_query != safe_query

                clauses: list[str] = []
                if "title" in wanted_fields:
                    clauses.append(f'title ~ "{title_query}"')
                if "text" in wanted_fields:
                    clauses.append(f'text ~ "{safe_query}"')
                parts.append(
                    f"({clauses[0]})" if len(clauses) == 1 else "(" + " OR ".join(clauses) + ")"
                )
                used_strategy = (
                    "title+text"
                    if wanted_fields == {"title", "text"}
                    else next(iter(wanted_fields))
                )
            else:
                used_strategy = "filter_only"

            if space_key:
                if not re.fullmatch(r"[A-Za-z0-9]{1,255}", space_key):
                    raise ValueError(f"invalid space_key: {space_key!r}")
                parts.append(f"space={space_key}")

            if under_page_id:
                if not re.fullmatch(r"\d{1,20}", str(under_page_id)):
                    raise ValueError(f"invalid under_page_id (must be numeric): {under_page_id!r}")
                parts.append(f"ancestor={under_page_id}")

            if author:
                # Atlassian CQL: `creator = "user"`. Escape quotes.
                safe_author = str(author).replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'creator = "{safe_author}"')

            if updated_since:
                # CQL date literal: "YYYY-MM-DD". Require ISO date.
                if not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?", str(updated_since)
                ):
                    raise ValueError(
                        f"updated_since must be ISO date (YYYY-MM-DD): {updated_since!r}"
                    )
                parts.append(f'lastModified >= "{updated_since}"')

            effective_cql = " AND ".join(parts)

        # ---------- 2. Execute ----------
        limit = max(1, min(int(limit), 25))
        # Log CQL at debug — truncated to avoid blowing the log line on
        # very long queries but full enough for correlation with API logs.
        logger.debug(
            "confluence.search strategy=%s cql=%r limit=%d",
            used_strategy,
            effective_cql[:300],
            limit,
        )
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/content/search",
                params={
                    "cql": effective_cql,
                    "limit": limit,
                    "expand": "body.view,space,version,ancestors",
                },
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()

        data = resp.json()
        hits: list[dict] = []
        for page in data.get("results", []):
            body_html = page.get("body", {}).get("view", {}).get("value", "")
            excerpt = _excerpt_from_html(body_html, max_chars=800)
            base_url = data.get("_links", {}).get(
                "base",
                f"https://{self.base_url.split('/wiki')[0].split('//')[1]}",
            )
            web_link = page.get("_links", {}).get("webui", "")
            raw_ancestors = page.get("ancestors") or []
            ancestors = [
                {"id": str(a.get("id", "")), "title": a.get("title", "")}
                for a in raw_ancestors
                if a.get("id")
            ]
            parent_id = ancestors[-1]["id"] if ancestors else ""
            parent_title = ancestors[-1]["title"] if ancestors else ""
            hits.append(
                {
                    "id": page["id"],
                    "title": page["title"],
                    "space": page.get("space", {}).get("name", ""),
                    "space_key": page.get("space", {}).get("key", ""),
                    "url": f"{base_url}{web_link}" if web_link else "",
                    "excerpt": excerpt,
                    "last_modified": page.get("version", {}).get("when", ""),
                    "parent_id": parent_id,
                    "parent_title": parent_title,
                    "ancestors": ancestors,
                }
            )

        # ---------- 3. Diagnostics for self-correction ----------
        diagnostics: dict[str, Any] = {
            "strategy": used_strategy,
            "brackets_stripped": brackets_stripped,
        }
        if not hits:
            # Give the model specific, actionable next steps based on what
            # it tried, not a generic "try again". This is the agentic
            # part — expose the CQL and the decision tree.
            hint_parts: list[str] = []
            if used_strategy == "title+text":
                hint_parts.append(
                    "Tried title+text with this query and got 0. "
                    "If the page definitely exists, try: (a) fields=['title'] "
                    "with a shorter phrase (1-3 key tokens); (b) drop space_key "
                    "to search all spaces; (c) use list_spaces → list_children "
                    "if you know roughly where the page lives; (d) raw cql "
                    "with exact title ='<exact title>'."
                )
            elif used_strategy == "title":
                hint_parts.append(
                    "Title-only search returned 0. The title may contain "
                    "different exact characters (brackets, punctuation, "
                    "language variant). Try fields=['text'] or a broader query."
                )
            elif used_strategy == "text":
                hint_parts.append(
                    "Body-only search returned 0. The page may be title-only "
                    "(e.g. a schedule with a table and no prose). Try fields=['title']."
                )
            elif used_strategy == "raw_cql":
                hint_parts.append(
                    "Raw CQL returned 0. Re-check operator syntax and field "
                    "names. Confluence CQL is documented at "
                    "https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/"
                )
            if space_key and used_strategy != "raw_cql":
                hint_parts.append(
                    f"Current space filter: space={space_key}. Removing it widens the search."
                )
            diagnostics["hint"] = (
                " ".join(hint_parts) if hint_parts else "No results; try broadening the query."
            )

        return {
            "hits": hits,
            "count": len(hits),
            "cql_used": effective_cql,
            "diagnostics": diagnostics,
        }

    # ─── Space operations (V2 API) ─────────────────────────────────────

    async def list_spaces(
        self,
        *,
        query: str | None = None,
        keys: list[str] | None = None,
        space_type: str | None = None,
        labels: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """List workspaces (spaces) via V2 API. Supports server-side
        filtering by keys/type/labels; `query` does client-side
        case-insensitive match against name/key/description.

        V2 doesn't expose a name search, so the pattern is:
        1. Pull one page of up to `limit` spaces with server-side filters
        2. Rank/filter client-side by `query` if provided

        For tenants with >250 spaces the caller should supply keys or
        labels to narrow server-side before relying on client match.
        """
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 250)),
            "include-icon": "false",
            "description-format": "plain",
        }
        if keys:
            # V2 accepts comma-separated list.
            params["keys"] = ",".join(k.strip() for k in keys if k.strip())
        if space_type:
            params["type"] = space_type
        if labels:
            params["labels"] = ",".join(label.strip() for label in labels if label.strip())

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.v2_base_url}/spaces",
                params=params,
                headers={"Authorization": self.auth_header, "Accept": "application/json"},
            )
            resp.raise_for_status()

        data = resp.json()
        raw = data.get("results", []) or []
        spaces = [self._normalize_space_v2(s) for s in raw]

        if query:
            spaces = _filter_and_rank_spaces(spaces, query)

        return spaces

    async def get_space(
        self, *, space_id: str | None = None, space_key: str | None = None
    ) -> dict | None:
        """Get a single space by numeric V2 id (preferred) or V1 key.

        V2's GET /spaces/{id} needs a numeric id. If the caller only has
        a key, we list-and-filter (keys= is server-side) to resolve.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            if space_id:
                resp = await client.get(
                    f"{self.v2_base_url}/spaces/{space_id}",
                    params={"description-format": "plain", "include-icon": "false"},
                    headers={
                        "Authorization": self.auth_header,
                        "Accept": "application/json",
                    },
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return self._normalize_space_v2(resp.json())

            if space_key:
                # Resolve by key via the list endpoint — V2 supports keys filter.
                resp = await client.get(
                    f"{self.v2_base_url}/spaces",
                    params={
                        "keys": space_key,
                        "limit": 1,
                        "description-format": "plain",
                        "include-icon": "false",
                    },
                    headers={
                        "Authorization": self.auth_header,
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                return self._normalize_space_v2(results[0]) if results else None

        return None

    def _normalize_space_v2(self, space: dict) -> dict:
        """Flatten a V2 space payload into a stable dict for tool output.
        Description comes in as either `{plain: {value}}` or a string depending
        on the description-format query; normalize both shapes."""
        desc_raw = space.get("description")
        if isinstance(desc_raw, dict):
            # {plain: {value, representation}} or {view: {value}}
            desc = (
                desc_raw.get("plain", {}).get("value")
                or desc_raw.get("view", {}).get("value")
                or ""
            )
        else:
            desc = desc_raw or ""

        space_key = space.get("key", "")
        space_id = space.get("id", "")
        web_path = space.get("_links", {}).get("webui") or f"/spaces/{space_key}"
        return {
            "id": str(space_id) if space_id else "",
            "key": space_key,
            "name": space.get("name", ""),
            "type": space.get("type", ""),
            "status": space.get("status", ""),
            "description": (desc or "").strip()[:500],
            "homepage_id": space.get("homepageId", ""),
            "url": f"https://{self.domain}/wiki{web_path}" if web_path else "",
            "created_at": space.get("createdAt", ""),
        }

    async def read_page(self, page_id: str | None = None, title: str | None = None) -> dict | None:
        """Read a single page by ID or title.

        Expands ancestors so the caller knows the page's parent — required
        for creating sibling pages (parent_id = this page's parent_id).
        """
        expand = "body.view,space,version,ancestors"
        async with httpx.AsyncClient(timeout=15) as client:
            if page_id:
                resp = await client.get(
                    f"{self.base_url}/content/{page_id}",
                    params={"expand": expand},
                    headers={"Authorization": self.auth_header},
                )
            elif title:
                resp = await client.get(
                    f"{self.base_url}/content",
                    params={"title": title, "expand": expand, "limit": 1},
                    headers={"Authorization": self.auth_header},
                )
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    if not results:
                        return None
                    page_data = results[0]
                    return self._parse_page(page_data)
                resp.raise_for_status()
                return None
            else:
                return None

            resp.raise_for_status()
            return self._parse_page(resp.json())

    def _parse_page(self, page: dict) -> dict:
        body_html = page.get("body", {}).get("view", {}).get("value", "")
        # Structure-preserving conversion (keeps bullets/headings/links) +
        # a 20K cap — enough for a long schedule or spec page without
        # blowing the context window. The agent loop has its own
        # per-retrieval-tool cap downstream as a second safety net.
        text = _html_to_structured_text(body_html)
        if len(text) > 20_000:
            text = text[:20_000] + "\n…[truncated at 20K chars]"

        # Ancestors come ordered root-first. For sibling creation the caller
        # needs the *immediate* parent — that's the last ancestor. Also
        # surface the full chain so the model can render breadcrumbs / show
        # a path to the user when helpful.
        raw_ancestors = page.get("ancestors") or []
        ancestors = [
            {
                "id": str(a.get("id", "")),
                "title": a.get("title", ""),
                "type": a.get("type", ""),
            }
            for a in raw_ancestors
            if a.get("id")
        ]
        parent_id = ancestors[-1]["id"] if ancestors else ""
        parent_title = ancestors[-1]["title"] if ancestors else ""

        return {
            "id": page["id"],
            "title": page["title"],
            "space": page.get("space", {}).get("name", ""),
            "space_key": page.get("space", {}).get("key", ""),
            "content": text,
            "url": page.get("_links", {}).get("base", "") + page.get("_links", {}).get("webui", ""),
            "last_modified": page.get("version", {}).get("when", ""),
            "version": page.get("version", {}).get("number", 0),
            "parent_id": parent_id,
            "parent_title": parent_title,
            "ancestors": ancestors,
        }

    # ─── Write operations ───────────────────────────────────────────

    async def create_page(
        self,
        space_key: str,
        title: str,
        content: str,
        parent_id: str | None = None,
    ) -> dict:
        """Create a new Confluence page. Content is Markdown-ish plain text wrapped as storage format."""
        body = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": self._to_storage_html(content),
                    "representation": "storage",
                },
            },
        }
        if parent_id:
            body["ancestors"] = [{"id": parent_id}]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/content",
                json=body,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        base = data.get("_links", {}).get("base", "")
        webui = data.get("_links", {}).get("webui", "")
        return {
            "id": data["id"],
            "title": data["title"],
            "url": f"{base}{webui}" if base else webui,
            "version": data.get("version", {}).get("number", 1),
        }

    async def update_page(
        self,
        page_id: str,
        content: str,
        title: str | None = None,
    ) -> dict:
        """Update an existing Confluence page. Fetches current version + title first."""
        # Fetch current page for version + existing title
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/content/{page_id}",
                params={"expand": "version,space"},
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()
            current = resp.json()

            new_version = current.get("version", {}).get("number", 1) + 1
            use_title = title or current["title"]

            body = {
                "version": {"number": new_version},
                "title": use_title,
                "type": "page",
                "body": {
                    "storage": {
                        "value": self._to_storage_html(content),
                        "representation": "storage",
                    },
                },
            }

            resp = await client.put(
                f"{self.base_url}/content/{page_id}",
                json=body,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        base = data.get("_links", {}).get("base", "")
        webui = data.get("_links", {}).get("webui", "")
        return {
            "id": data["id"],
            "title": data["title"],
            "url": f"{base}{webui}" if base else webui,
            "version": data.get("version", {}).get("number", new_version),
        }

    async def add_comment(self, page_id: str, comment: str) -> dict:
        """Add a comment to a Confluence page."""
        body = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {
                "storage": {
                    "value": self._to_storage_html(comment),
                    "representation": "storage",
                },
            },
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/content",
                json=body,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "id": data["id"],
            "page_id": page_id,
            "created": data.get("version", {}).get("when", ""),
        }

    async def delete_page(self, page_id: str) -> dict:
        """Delete (trash) a Confluence page. This is a soft-delete; page goes to trash."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{self.base_url}/content/{page_id}",
                headers={"Authorization": self.auth_header},
            )
            if resp.status_code not in (200, 204):
                resp.raise_for_status()

        return {"id": page_id, "status": "trashed"}

    # ─── URL resolver + children + find_replace ────────────────────

    def resolve_url_to_page_id(self, url: str) -> str | None:
        """Extract page_id from a Confluence URL.

        Supports:
          - https://x.atlassian.net/wiki/spaces/SPACE/pages/1234567/Title
          - ?pageId=1234567 query form (legacy viewpage.action)
        Short URLs (/wiki/x/XYZ) are not resolvable without an API call —
        caller should use search + title fallback.
        """
        if not url:
            return None
        m = re.search(r"/pages/(\d+)(?:/|\?|$)", url)
        if m:
            return m.group(1)
        m = re.search(r"[?&]pageId=(\d+)", url, re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    async def list_children(self, page_id: str, limit: int = 25) -> list[dict]:
        """List direct child pages of a page (one level only)."""
        params = {
            "limit": max(1, min(int(limit), 100)),
            "expand": "version,_links",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/content/{page_id}/child/page",
                params=params,
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()

        data = resp.json()
        base_link = data.get("_links", {}).get("base") or f"https://{self.domain}/wiki"
        results = []
        for ch in data.get("results", []):
            web = ch.get("_links", {}).get("webui", "")
            results.append(
                {
                    "id": ch.get("id", ""),
                    "title": ch.get("title", ""),
                    "url": f"{base_link}{web}" if web else "",
                    "last_modified": ch.get("version", {}).get("when", ""),
                }
            )
        return results

    async def move_page(self, page_id: str, target_parent_id: str) -> dict:
        """Move a page under a different parent (same space).

        Atlassian's V1 API moves a page via a PUT that sets the
        `ancestors` array to `[{id: target_parent_id}]` (single element
        means "this is the immediate parent"). Version must bump.

        We validate target_parent_id exists first with a HEAD-like GET to
        catch typos before mutating. Refuses no-op (moving to current parent).
        """
        if not page_id or not target_parent_id:
            raise ValueError("move_page requires page_id and target_parent_id")
        if page_id == target_parent_id:
            raise ValueError("cannot move a page under itself")

        async with httpx.AsyncClient(timeout=15) as client:
            # Fetch current state: version + title + existing parent + space
            resp = await client.get(
                f"{self.base_url}/content/{page_id}",
                params={"expand": "version,ancestors,space"},
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()
            current = resp.json()
            current_version = current.get("version", {}).get("number", 1)
            current_title = current.get("title", "")
            current_parents = current.get("ancestors") or []
            current_parent_id = str(current_parents[-1].get("id", "")) if current_parents else ""
            if current_parent_id == target_parent_id:
                raise ValueError(
                    f"page {page_id} is already under parent {target_parent_id} — nothing to move"
                )

            # Validate target exists to avoid leaving the page in a bad state.
            validate = await client.get(
                f"{self.base_url}/content/{target_parent_id}",
                params={"expand": "space"},
                headers={"Authorization": self.auth_header},
            )
            if validate.status_code == 404:
                raise ValueError(
                    f"target parent {target_parent_id} does not exist — check the id first"
                )
            validate.raise_for_status()
            target = validate.json()
            current_space_key = current.get("space", {}).get("key", "")
            target_space_key = target.get("space", {}).get("key", "")
            if current_space_key and target_space_key and current_space_key != target_space_key:
                raise ValueError(
                    f"cross-space moves not supported: page is in {current_space_key!r}, "
                    f"target parent is in {target_space_key!r}"
                )

            body = {
                "version": {"number": current_version + 1},
                "title": current_title,
                "type": "page",
                "ancestors": [{"id": target_parent_id}],
            }
            resp = await client.put(
                f"{self.base_url}/content/{page_id}",
                json=body,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        base = data.get("_links", {}).get("base", "")
        webui = data.get("_links", {}).get("webui", "")
        return {
            "id": data["id"],
            "title": data.get("title", current_title),
            "url": f"{base}{webui}" if base else webui,
            "version": data.get("version", {}).get("number", current_version + 1),
            "old_parent_id": current_parent_id,
            "new_parent_id": target_parent_id,
        }

    async def find_and_replace_in_page(
        self, page_id: str, find: str, replace: str, *, raw_html: bool = False
    ) -> dict:
        """Safe partial edit. Requires `find` to match EXACTLY ONCE, raises
        ValueError for 0 or >1 matches so the model corrects itself rather
        than producing a bad edit.

        Operates on Confluence storage-format XHTML. Model should first
        `read_page` to see the exact text before composing `find`.

        By default `replace` is treated as plain text and escaped for XHTML
        (bare `&`, `<`, `>` become entities) so a replacement like
        "A & B" doesn't corrupt page markup. Set `raw_html=True` when the
        replacement IS meant to be XHTML (inserting `<strong>` etc.).
        """
        if not find:
            raise ValueError("`find` must be non-empty")
        if replace is None:
            raise ValueError("`replace` must not be None")
        if not raw_html:
            replace = _escape_for_storage(replace)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/content/{page_id}",
                params={"expand": "body.storage,version,space"},
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()
            current = resp.json()
            current_body = current.get("body", {}).get("storage", {}).get("value") or ""
            current_version = current.get("version", {}).get("number", 1)
            current_title = current.get("title", "")

            count = current_body.count(find)
            if count == 0:
                raise ValueError(
                    f"`find` string not found on page {page_id}. "
                    "Check exact casing/spacing. Tip: call read_page first."
                )
            if count > 1:
                raise ValueError(
                    f"`find` string appears {count} times on page {page_id} — "
                    "refusing ambiguous edit. Narrow with surrounding context so "
                    "it matches exactly once."
                )

            new_body = current_body.replace(find, replace, 1)
            body = {
                "version": {"number": current_version + 1},
                "title": current_title,
                "type": "page",
                "body": {
                    "storage": {
                        "value": new_body,
                        "representation": "storage",
                    },
                },
            }
            resp = await client.put(
                f"{self.base_url}/content/{page_id}",
                json=body,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        base = data.get("_links", {}).get("base", "")
        webui = data.get("_links", {}).get("webui", "")
        return {
            "id": data["id"],
            "title": data.get("title", current_title),
            "url": f"{base}{webui}" if base else webui,
            "version": data.get("version", {}).get("number", current_version + 1),
            "chars_before": len(current_body),
            "chars_after": len(new_body),
        }

    @staticmethod
    def _to_storage_html(content: str) -> str:
        """Convert markdown to Confluence storage-format XHTML.
        Delegates to module-level `_markdown_to_storage` (testable directly)."""
        return _markdown_to_storage(content)


# ─── Tool Executors ──────────────────────────────────────────────────
#
# Two meta-executors dispatch on the `action` parameter. This replaces
# eight separate single-purpose executors — same capabilities, ~80%
# fewer tokens in the tool-schema catalog the model sees each turn.


def _err(request: ToolCallRequest, message: str, start: float) -> ToolCallResult:
    return ToolCallResult(
        call_id=request.call_id,
        tool_name=request.tool_name,
        success=False,
        error=message,
        duration_ms=(time.time() - start) * 1000,
    )


# Always-appended reminder on error paths so the model reports the
# failure honestly instead of hallucinating success. Short because it
# rides along on every error message.
_ANTI_HALLUCINATION_NOTE = (
    " | ⚠️ This call FAILED. Do not tell the user the action succeeded — "
    "explain what went wrong and what they should do next."
)


def _classify_http_error(status_code: int, action: str | None = None) -> str:
    """Map HTTP status to a short human explanation the model can relay.
    Each message includes the failure reminder so hallucinated 'success'
    responses stop happening on these paths."""
    action_text = f" for action={action!r}" if action else ""
    if status_code == 401:
        msg = (
            f"Authentication failed{action_text} (HTTP 401). "
            "The stored API token is invalid or expired — ask the user to "
            "re-connect Confluence in the integrations panel."
        )
    elif status_code == 403:
        msg = (
            f"Permission denied{action_text} (HTTP 403). "
            "The connected account doesn't have access to this page/space. "
            "Check that the user's Confluence account has the right role."
        )
    elif status_code == 404:
        msg = (
            f"Resource not found{action_text} (HTTP 404). "
            "The page_id / space_key / target_parent_id does not exist, or "
            "the account can't see it. Verify the id and try again."
        )
    elif status_code == 409:
        msg = (
            f"Version conflict{action_text} (HTTP 409). "
            "Someone else updated this page since you read it — read it "
            "again and retry with the new version."
        )
    elif status_code == 429:
        msg = (
            f"Rate limited{action_text} (HTTP 429). "
            "Too many requests — back off and try again in a moment."
        )
    else:
        msg = f"Confluence API error (HTTP {status_code}){action_text}."
    return msg + _ANTI_HALLUCINATION_NOTE


def _format_search_results(search_result: dict, query: str) -> str:
    """Render the structured `search` return into a model-readable block.

    Always prints the CQL that was executed so the model can reason about
    what just happened. On empty results, the diagnostic hint is the
    model's lifeline — we put it prominently at the top.
    """
    hits = search_result.get("hits") or []
    cql_used = search_result.get("cql_used", "")
    diag = search_result.get("diagnostics") or {}
    strategy = diag.get("strategy", "?")

    if not hits:
        lines = [f"No pages matched. CQL executed: `{cql_used}`"]
        if diag.get("hint"):
            lines.append(f"\n**Next step suggestion:** {diag['hint']}")
        return "\n".join(lines)

    parts = [f"Found {len(hits)} Confluence page(s) (strategy={strategy}, cql=`{cql_used}`):"]
    for r in hits:
        parent_id = r.get("parent_id", "")
        parent_title = r.get("parent_title", "")
        parent_line = (
            f"Parent: {parent_title} (id: {parent_id})"
            if parent_id
            else "Parent: (top-level — space homepage)"
        )
        parts.append(
            f"\n**{r.get('title', '')}** (Space: {r.get('space', '')}, key: {r.get('space_key', '')})\n"
            f"ID: {r.get('id', '')} | URL: {r.get('url', '')}\n"
            f"{parent_line}\n"
            f"{r.get('excerpt', '')}"
        )
    return "\n---\n".join(parts) if len(parts) > 1 else parts[0]


def _format_spaces(spaces: list[dict], query: str | None) -> str:
    if not spaces:
        return (
            f"No Confluence spaces matched query={query!r}. "
            "Try listing without a query, or different keywords."
        )
    header = (
        f"Found {len(spaces)} Confluence space(s)" + (f" matching '{query}'" if query else "") + ":"
    )
    lines = [header]
    for s in spaces[:25]:
        desc = (s.get("description") or "").strip()
        desc_line = f"\n  {desc[:200]}" if desc else ""
        lines.append(
            f"- **{s.get('name', '')}** (key: `{s.get('key', '')}`, "
            f"id: {s.get('id', '')}, type: {s.get('type', '')})"
            f"{desc_line}\n  URL: {s.get('url', '')}"
        )
    if len(spaces) > 25:
        lines.append(f"\n…and {len(spaces) - 25} more. Narrow with `query` or `labels`.")
    return "\n".join(lines)


def _format_children(children: list[dict], page_id: str) -> str:
    if not children:
        return f"Page {page_id} has no direct child pages."
    lines = [f"Found {len(children)} child page(s) of {page_id}:"]
    for ch in children:
        lines.append(
            f"- **{ch.get('title', '')}** (id: {ch.get('id', '')})\n  URL: {ch.get('url', '')}"
        )
    return "\n".join(lines)


class _TenantClientResolver:
    """Resolves a `ConfluenceAPIClient` for each call, keyed by the
    caller's tenant_id.

    Why this exists: `register_confluence_tools` runs process-globally and
    only stores ONE executor instance. If we close over one tenant's
    credentials at registration time, every tenant's request hits that
    same Confluence workspace — a cross-tenant credential leak.

    So instead, the executor stores a *database reference* (+ a one-time
    fallback client for legacy single-tenant deployments & tests). At
    call time `resolve(request)` reads `request.user.tenant_id`, queries
    `confluence_connections` for that tenant's active row, and returns a
    client built from those credentials.

    We cache per-tenant clients for a short TTL (60s) so we don't slam
    the DB on every tool call during a multi-iteration agent turn.
    """

    _CACHE_TTL_SECONDS = 60.0

    def __init__(
        self,
        database: Any = None,
        fallback_client: ConfluenceAPIClient | None = None,
        credential_repository: Any = None,
        secret_resolver: Any = None,
    ):
        self._database = database
        self._fallback = fallback_client
        self._credential_repository = credential_repository
        self._secret_resolver = secret_resolver
        self._cache: dict[str, tuple[float, ConfluenceAPIClient]] = {}

    def _cache_get(self, tenant_id: str) -> ConfluenceAPIClient | None:
        entry = self._cache.get(tenant_id)
        if entry is None:
            return None
        ts, client = entry
        if (time.time() - ts) > self._CACHE_TTL_SECONDS:
            self._cache.pop(tenant_id, None)
            return None
        return client

    def _cache_put(self, tenant_id: str, client: ConfluenceAPIClient) -> None:
        self._cache[tenant_id] = (time.time(), client)

    async def resolve(self, request: ToolCallRequest) -> ConfluenceAPIClient:
        """Return the ConfluenceAPIClient the caller should use.

        Resolution order:
          1. request.user.tenant_id → DB lookup → per-tenant client (CACHED).
          2. tenant_id in request.metadata → DB lookup (rarely used; kept for
             backwards compat with call sites that don't wire UserContext).
          3. Fallback client (legacy single-tenant ctor / tests).
        If none resolve, raise ValueError with a message the model can relay.
        """
        tenant_id = ""
        user = getattr(request, "user", None)
        if user is not None:
            tenant_id = getattr(user, "tenant_id", "") or ""
        if not tenant_id:
            tenant_id = str((request.metadata or {}).get("tenant_id") or "")

        metadata = request.metadata or {}
        agent_runtime = bool(metadata.get("agent_id") or metadata.get("agent_version_id"))
        principal = metadata.get("connector_principal")
        if agent_runtime:
            if (
                not isinstance(principal, dict)
                or self._credential_repository is None
                or self._secret_resolver is None
            ):
                raise ValueError("Connector credential principal is unavailable.")
            user_id = str(metadata.get("user_id") or getattr(user, "user_id", "") or "")
            authenticated = bool(getattr(user, "is_authenticated", False))
            row = await self._credential_repository.authorize_connector_tool(
                tenant_id=tenant_id,
                user_id=user_id,
                authenticated=authenticated,
                provider=str(principal.get("provider") or ""),
                tool_name=request.tool_name,
                principal_type=str(principal.get("principal_type") or ""),
                grant_id=str(principal.get("grant_id") or ""),
                channel=str(principal.get("channel") or ""),
            )
            token = await self._secret_resolver.resolve(str(row.get("secret_ref") or ""))
            connection_metadata = row.get("connection_metadata") or {}
            domain = str(connection_metadata.get("domain") or "").strip()
            email = str(connection_metadata.get("email") or "").strip()
            if not domain or not email or not token:
                raise ValueError("Connector credential principal is unavailable.")
            return ConfluenceAPIClient(domain, email, token)

        if tenant_id and self._database is not None:
            cached = self._cache_get(tenant_id)
            if cached is not None:
                return cached
            try:
                rows = await self._database.list_confluence_connections(
                    tenant_id=tenant_id, status="active", limit=1
                )
            except Exception:
                logger.exception(
                    "confluence: DB lookup failed for tenant %s — falling back",
                    tenant_id,
                )
                rows = []
            if rows:
                row = rows[0]
                domain = (row.get("domain") or "").strip()
                email = (row.get("email") or "").strip()
                token = (row.get("api_token") or "").strip()
                if domain and email and token:
                    client = ConfluenceAPIClient(domain, email, token)
                    self._cache_put(tenant_id, client)
                    return client

        if self._fallback is not None and self._fallback.domain:
            # Only use fallback if it has real creds (avoids the empty
            # placeholder from register_confluence_tools() without args).
            return self._fallback

        raise ValueError(
            "No active Confluence connection for this tenant. Ask the user "
            "to connect Confluence via the Integrations panel, or ensure "
            f"the tenant row exists in confluence_connections (tenant_id={tenant_id!r})."
        )


class ConfluenceReadExecutor(ToolExecutor):
    """Dispatches read-only Confluence operations by `action`.

    Two ways to construct (in priority order when both provided):
      - `database=...`: per-call tenant-scoped credential lookup (production).
      - `client=...`: static client (tests and legacy single-tenant mode).
    """

    def __init__(
        self,
        client: ConfluenceAPIClient | None = None,
        database: Any = None,
        credential_repository: Any = None,
        secret_resolver: Any = None,
    ):
        self._resolver = _TenantClientResolver(
            database=database,
            fallback_client=client,
            credential_repository=credential_repository,
            secret_resolver=secret_resolver,
        )
        # `self.client` kept as a shim for legacy tests that poke at
        # `executor.client` directly — always returns the fallback if present.
        self.client = client  # may be None when DB-only

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        args = request.arguments or {}
        action = str(args.get("action") or "").strip()
        if not action:
            return _err(
                request,
                "`action` is required (search/read_page/list_spaces/get_space/list_children)",
                start,
            )

        # Resolve the correct per-tenant client BEFORE dispatch so we never
        # accidentally use another tenant's credentials.
        try:
            client = await self._resolver.resolve(request)
        except ValueError as ve:
            return _err(request, str(ve) + _ANTI_HALLUCINATION_NOTE, start)

        try:
            if action == "search":
                query = str(args.get("query") or "").strip()
                cql_raw = args.get("cql") or None
                if (
                    not query
                    and not cql_raw
                    and not (
                        args.get("author") or args.get("updated_since") or args.get("under_page_id")
                    )
                ):
                    return _err(
                        request,
                        "`search` needs at least one of: query, cql, author, updated_since, or under_page_id",
                        start,
                    )
                fields = args.get("fields")
                if isinstance(fields, str):
                    fields = [f.strip() for f in fields.split(",") if f.strip()]
                space_key = args.get("space_key") or None
                author = args.get("author") or None
                updated_since = args.get("updated_since") or None
                under_page_id = args.get("under_page_id") or None
                limit = max(1, min(int(args.get("limit") or 10), 25))
                try:
                    search_result = await client.search(
                        query=query,
                        space_key=space_key,
                        fields=fields,
                        author=author,
                        updated_since=updated_since,
                        under_page_id=under_page_id,
                        cql=cql_raw,
                        limit=limit,
                    )
                except ValueError as ve:
                    # Validation errors — surface cleanly so the model can
                    # see exactly which parameter was rejected and retry.
                    return _err(request, str(ve), start)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=_format_search_results(search_result, query),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={
                        "count": search_result.get("count", 0),
                        "query": query,
                        "cql_used": search_result.get("cql_used", ""),
                        "strategy": search_result.get("diagnostics", {}).get("strategy"),
                    },
                )

            if action == "read_page":
                page_id = (args.get("page_id") or "").strip() or None
                title = (args.get("title") or "").strip() or None
                url = (args.get("url") or "").strip() or None
                if url and not page_id:
                    page_id = client.resolve_url_to_page_id(url)
                    if not page_id:
                        return _err(
                            request,
                            f"Could not extract page_id from URL {url!r}. "
                            "Short URLs aren't resolvable — try search with title keywords.",
                            start,
                        )
                if not page_id and not title:
                    return _err(request, "`read_page` requires page_id, title, or url", start)
                page = await client.read_page(page_id=page_id, title=title)
                if not page:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=True,
                        result=f"No page found (page_id={page_id!r}, title={title!r}).",
                        duration_ms=(time.time() - start) * 1000,
                        metadata={"found": False},
                    )
                content = page.get("content") or ""
                parent_id = page.get("parent_id") or ""
                parent_title = page.get("parent_title") or ""
                ancestors = page.get("ancestors") or []

                # Build breadcrumb so the model sees "where am I in the tree"
                # and can pick the right parent_id for a sibling create_page.
                if ancestors:
                    breadcrumb = " > ".join(
                        f"{a.get('title', '')} ({a.get('id', '')})" for a in ancestors
                    )
                    parent_line = (
                        f"Parent: {parent_title} (id: {parent_id})\n"
                        f"Path: {breadcrumb} > {page.get('title', '')}\n"
                    )
                else:
                    parent_line = "Parent: (none — this is a space homepage or top-level page)\n"

                result_text = (
                    f"**{page.get('title', '')}** (space: {page.get('space', '')}, "
                    f"id: {page.get('id', '')})\n"
                    f"URL: {page.get('url', '')}\n"
                    f"{parent_line}"
                    f"Last modified: {page.get('last_modified', '')}\n\n"
                    f"{content}"
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=result_text,
                    duration_ms=(time.time() - start) * 1000,
                    metadata={
                        "found": True,
                        "page_id": page.get("id"),
                        "parent_id": parent_id,
                        "space_key": page.get("space_key"),
                        "chars": len(content),
                    },
                )

            if action == "list_spaces":
                query = (args.get("query") or "").strip() or None
                keys = args.get("keys") or None
                if isinstance(keys, str):
                    keys = [k.strip() for k in keys.split(",") if k.strip()]
                space_type = args.get("space_type") or None
                labels = args.get("labels") or None
                if isinstance(labels, str):
                    labels = [label.strip() for label in labels.split(",") if label.strip()]
                try:
                    limit = max(1, min(int(args.get("limit") or 50), 250))
                except (TypeError, ValueError):
                    limit = 50
                spaces = await client.list_spaces(
                    query=query, keys=keys, space_type=space_type, labels=labels, limit=limit
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=_format_spaces(spaces, query),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"count": len(spaces), "query": query, "shown": min(len(spaces), 25)},
                )

            if action == "get_space":
                space_id = (args.get("space_id") or "").strip() or None
                space_key = (args.get("space_key") or "").strip() or None
                if not space_id and not space_key:
                    return _err(request, "`get_space` requires space_id or space_key", start)
                space = await client.get_space(space_id=space_id, space_key=space_key)
                if not space:
                    return ToolCallResult(
                        call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=True,
                        result=f"No space found (id={space_id!r}, key={space_key!r}).",
                        duration_ms=(time.time() - start) * 1000,
                        metadata={"found": False},
                    )
                desc = space.get("description") or "(no description)"
                body_text = (
                    f"**{space.get('name', '')}** (key: `{space.get('key', '')}`, "
                    f"id: {space.get('id', '')}, type: {space.get('type', '')})\n"
                    f"URL: {space.get('url', '')}\n"
                    f"Homepage ID: {space.get('homepage_id') or '(none)'}\n"
                    f"Created: {space.get('created_at') or '(unknown)'}\n"
                    f"Description: {desc}"
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=body_text,
                    duration_ms=(time.time() - start) * 1000,
                    metadata={
                        "found": True,
                        "space_id": space.get("id"),
                        "space_key": space.get("key"),
                    },
                )

            if action == "list_children":
                page_id = (args.get("page_id") or "").strip()
                if not page_id:
                    return _err(request, "`list_children` requires page_id", start)
                try:
                    limit = max(1, min(int(args.get("limit") or 25), 100))
                except (TypeError, ValueError):
                    limit = 25
                children = await client.list_children(page_id, limit=limit)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=_format_children(children, page_id),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"count": len(children), "page_id": page_id},
                )

            return _err(request, f"Unknown action {action!r}", start)

        except ValueError as e:
            # Expected validation errors — surface to model cleanly.
            return _err(request, str(e) + _ANTI_HALLUCINATION_NOTE, start)
        except httpx.HTTPStatusError as e:
            logger.error(
                "confluence_read action=%s HTTP %s",
                action,
                e.response.status_code,
            )
            return _err(request, _classify_http_error(e.response.status_code, action), start)
        except Exception:
            logger.exception("confluence_read action=%s failed", action)
            return _err(
                request,
                "confluence_read failed: upstream unavailable" + _ANTI_HALLUCINATION_NOTE,
                start,
            )


class ConfluenceWriteExecutor(ToolExecutor):
    """Dispatches write Confluence operations by `action`. Gated by
    `requires_confirmation=True` in the definition — the permission
    middleware yields a `confirm` verdict before execution.

    See `ConfluenceReadExecutor` for the two-arg ctor rationale."""

    def __init__(
        self,
        client: ConfluenceAPIClient | None = None,
        database: Any = None,
        credential_repository: Any = None,
        secret_resolver: Any = None,
    ):
        self._resolver = _TenantClientResolver(
            database=database,
            fallback_client=client,
            credential_repository=credential_repository,
            secret_resolver=secret_resolver,
        )
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        args = request.arguments or {}
        action = str(args.get("action") or "").strip()
        if not action:
            return _err(
                request,
                "`action` is required (create_page/update_page/find_replace/comment/delete_page)",
                start,
            )

        # Resolve per-tenant client — never trust a process-global one.
        try:
            client = await self._resolver.resolve(request)
        except ValueError as ve:
            return _err(request, str(ve) + _ANTI_HALLUCINATION_NOTE, start)

        try:
            if action == "create_page":
                space_key = (args.get("space_key") or "").strip()
                title = (args.get("title") or "").strip()
                content = args.get("content") or ""
                if not space_key or not title:
                    return _err(request, "`create_page` requires space_key and title", start)
                parent_id = (args.get("parent_id") or "").strip() or None
                page = await client.create_page(
                    space_key=space_key, title=title, content=content, parent_id=parent_id
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=(
                        f"Created Confluence page:\n"
                        f"- title: {page.get('title', title)}\n"
                        f"- id: {page.get('id', '')}\n"
                        f"- url: {page.get('url', '')}\n"
                        f"- version: {page.get('version', 1)}"
                    ),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"page_id": page.get("id"), "action": "create_page"},
                )

            if action == "update_page":
                page_id = (args.get("page_id") or "").strip()
                content = args.get("content")
                if not page_id or content is None:
                    return _err(request, "`update_page` requires page_id and content", start)
                title = (args.get("title") or "").strip() or None
                page = await client.update_page(page_id=page_id, content=content, title=title)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=(
                        f"Updated Confluence page {page_id} to version {page.get('version', '?')}:\n"
                        f"- title: {page.get('title', '')}\n"
                        f"- url: {page.get('url', '')}"
                    ),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"page_id": page_id, "action": "update_page"},
                )

            if action == "find_replace":
                page_id = (args.get("page_id") or "").strip()
                find = args.get("find") or ""
                replace = args.get("replace")
                if not page_id or not find or replace is None:
                    return _err(
                        request,
                        "`find_replace` requires page_id, find (non-empty), replace",
                        start,
                    )
                raw_html = bool(args.get("raw_html"))
                page = await client.find_and_replace_in_page(
                    page_id=page_id, find=find, replace=replace, raw_html=raw_html
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=(
                        f"Applied find_replace to page {page_id}:\n"
                        f"- chars before: {page.get('chars_before')}, after: {page.get('chars_after')}\n"
                        f"- new version: {page.get('version')}\n"
                        f"- url: {page.get('url', '')}"
                    ),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"page_id": page_id, "action": "find_replace"},
                )

            if action == "move_page":
                page_id = (args.get("page_id") or "").strip()
                target_parent_id = (args.get("target_parent_id") or "").strip()
                if not page_id or not target_parent_id:
                    return _err(
                        request,
                        "`move_page` requires page_id and target_parent_id",
                        start,
                    )
                r = await client.move_page(page_id=page_id, target_parent_id=target_parent_id)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=(
                        f"Moved page {page_id}:\n"
                        f"- old parent: {r.get('old_parent_id') or '(top-level)'}\n"
                        f"- new parent: {r.get('new_parent_id')}\n"
                        f"- new version: {r.get('version')}\n"
                        f"- url: {r.get('url', '')}"
                    ),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={
                        "page_id": page_id,
                        "action": "move_page",
                        "old_parent_id": r.get("old_parent_id"),
                        "new_parent_id": r.get("new_parent_id"),
                    },
                )

            if action == "comment":
                page_id = (args.get("page_id") or "").strip()
                body_text = args.get("body") or args.get("comment") or ""
                if not page_id or not body_text:
                    return _err(request, "`comment` requires page_id and body", start)
                r = await client.add_comment(page_id=page_id, comment=body_text)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=(
                        f"Added comment to page {page_id}:\n"
                        f"- comment id: {r.get('id', '')}\n"
                        f"- created: {r.get('created', '')}"
                    ),
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"page_id": page_id, "action": "comment"},
                )

            if action == "delete_page":
                page_id = (args.get("page_id") or "").strip()
                if not page_id:
                    return _err(request, "`delete_page` requires page_id", start)
                r = await client.delete_page(page_id=page_id)
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=f"Page {page_id} moved to trash (status: {r.get('status', 'trashed')}).",
                    duration_ms=(time.time() - start) * 1000,
                    metadata={"page_id": page_id, "action": "delete_page"},
                )

            return _err(request, f"Unknown action {action!r}", start)

        except ValueError as e:
            # Validation errors — the model can correct its args and retry.
            # Still attach the hallucination guard.
            return _err(request, str(e) + _ANTI_HALLUCINATION_NOTE, start)
        except httpx.HTTPStatusError as e:
            logger.error(
                "confluence_write action=%s HTTP %s",
                action,
                e.response.status_code,
            )
            return _err(request, _classify_http_error(e.response.status_code, action), start)
        except Exception:
            logger.exception("confluence_write action=%s failed", action)
            return _err(
                request,
                "confluence_write failed: upstream unavailable" + _ANTI_HALLUCINATION_NOTE,
                start,
            )


# ─── Registration ─────────────────────────────────────────────────────


def _confluence_has_active_connection_factory(
    database: Any,
    static_client: ConfluenceAPIClient | None,
) -> Any:
    """Build the predicate used by the ConnectorRegistry to decide whether
    the Confluence tools should appear in *this* request's tool list.

    Activation rules (any one is enough):
      1. The request's tenant has an active row in ``confluence_connections``.
      2. A static client with real creds was supplied at registration time
         (legacy single-tenant mode / tests).

    Predicate results are cached 60s per tenant inside the ConnectorRegistry
    itself — no TTL bookkeeping needed here.
    """

    async def _predicate(request: ToolCallRequest) -> bool:
        # Static-client fallback: if credentials were baked in at registration
        # time, the connector is always visible — that's how the legacy
        # single-tenant deployment and respx-based tests work.
        if static_client is not None and static_client.domain:
            return True
        if database is None:
            return False
        user = getattr(request, "user", None)
        tenant_id = ""
        if user is not None:
            tenant_id = str(getattr(user, "tenant_id", "") or "")
        if not tenant_id:
            tenant_id = str((getattr(request, "metadata", None) or {}).get("tenant_id") or "")
        if not tenant_id:
            return False
        try:
            rows = await database.list_confluence_connections(
                tenant_id=tenant_id, status="active", limit=1
            )
        except Exception:
            logger.exception(
                "confluence connector predicate: DB lookup failed for tenant %s",
                tenant_id,
            )
            return False
        return bool(rows)

    return _predicate


def register_confluence_tools(
    domain: str = "",
    email: str = "",
    api_token: str = "",
    tenant_id: str = "",  # accepted for backwards compat; no longer consumed here
    database: Any = None,
    credential_repository: Any = None,
    secret_resolver: Any = None,
) -> None:
    """Register the 2 Confluence meta-tools (confluence_read + confluence_write).

    Connector-pattern registration (Phase A refactor):

    - The **executors** still live in the global ToolRegistry so any inbound
      tool call can be dispatched. (Execution path didn't change.)
    - The **tool definitions** live in the ConnectorRegistry behind a
      per-tenant predicate, so the model only sees `confluence_read/write`
      in its tool list when the caller's tenant has an active Confluence
      connection. This mirrors how Claude.ai surfaces Gmail/Drive
      connectors — cost: 0 tokens for tenants who never connected.

    Two credential modes, auto-selected:

    1. **Multi-tenant (production)**: pass ``database=<Database>``.
       Each tool call resolves credentials from ``confluence_connections``
       via ``request.user.tenant_id`` (no cross-tenant leak).
    2. **Single-tenant / legacy**: pass ``domain/email/api_token``.
       Used by ``/connectors/activate`` and by tests with a static client.

    Passing both is fine: DB lookup first, static fallback second.
    """
    static_client: ConfluenceAPIClient | None = None
    if domain and email and api_token:
        static_client = ConfluenceAPIClient(domain, email, api_token)

    if static_client is None and database is None:
        logger.warning(
            "Confluence tools registered with neither credentials nor "
            "database — all calls will fail until a connection is provided."
        )

    read_executor = ConfluenceReadExecutor(
        client=static_client,
        database=database,
        credential_repository=credential_repository,
        secret_resolver=secret_resolver,
    )
    write_executor = ConfluenceWriteExecutor(
        client=static_client,
        database=database,
        credential_repository=credential_repository,
        secret_resolver=secret_resolver,
    )

    # Definitions AND executors both live in the global ToolRegistry so the
    # runtime dispatch path (`tool_registry.execute(request)`) can always find
    # and run the tool when an inbound call arrives for an authorized tenant.
    # The agent loop is responsible for subtracting these tool names from the
    # per-request model tool-list when the ConnectorRegistry predicate says
    # "this tenant hasn't connected" — that's the visibility gate, distinct
    # from the execution gate.
    tool_registry = get_tool_registry()
    tool_registry.register(CONFLUENCE_READ_DEFINITION, read_executor, allow_override=True)
    tool_registry.register(CONFLUENCE_WRITE_DEFINITION, write_executor, allow_override=True)

    predicate = _confluence_has_active_connection_factory(database, static_client)
    get_connector_registry().register(
        connector_id="confluence",
        tool_defs=[CONFLUENCE_READ_DEFINITION, CONFLUENCE_WRITE_DEFINITION],
        predicate=predicate,
    )

    mode = (
        "db-backed (per-tenant)"
        if database is not None
        else (f"static ({domain})" if static_client else "uninitialized")
    )
    logger.info(
        f"Registered 2 Confluence meta-tools via ConnectorRegistry "
        f"(read + write, 11 actions total) — mode: {mode}"
    )
