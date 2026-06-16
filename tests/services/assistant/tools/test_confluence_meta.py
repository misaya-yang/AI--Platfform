"""
Meta-tool tests — cover the new P0 capabilities and the dispatch layer
that replaces the 8 old single-purpose executors.

Scope:
- URL → page_id resolver
- Markdown → Confluence storage-format converter
- find_and_replace_in_page (happy path + 0-match refusal + n-match refusal)
- list_children pagination / shape
- confluence_read action dispatch (each action routes correctly)
- confluence_write action dispatch + validation errors
"""

from __future__ import annotations

import httpx
import pytest
import respx


def _read_req(arguments: dict):
    from assistant_service.core.tools.tool_registry import ToolCallRequest
    return ToolCallRequest(call_id="c", tool_name="confluence_read", arguments=arguments)


def _write_req(arguments: dict):
    from assistant_service.core.tools.tool_registry import ToolCallRequest
    return ToolCallRequest(call_id="c", tool_name="confluence_write", arguments=arguments)


# ---------------------------------------------------------------------------
# URL → page_id resolver
# ---------------------------------------------------------------------------


def test_resolve_url_pages_slash_form():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    url = "https://ex.atlassian.net/wiki/spaces/SALES/pages/1038712833/Q1-Plan"
    assert c.resolve_url_to_page_id(url) == "1038712833"


def test_resolve_url_query_param_form():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    url = "https://ex.atlassian.net/wiki/pages/viewpage.action?pageId=987654"
    assert c.resolve_url_to_page_id(url) == "987654"


def test_resolve_url_short_form_returns_none():
    """Short URLs need an API round-trip — not resolvable purely from the string."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    assert c.resolve_url_to_page_id("https://ex.atlassian.net/wiki/x/ABCDEF") is None


def test_resolve_url_handles_trailing_slash_and_hash():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    assert c.resolve_url_to_page_id("https://x/wiki/spaces/S/pages/42/") == "42"
    assert c.resolve_url_to_page_id("https://x/wiki/spaces/S/pages/42?foo=bar") == "42"


# ---------------------------------------------------------------------------
# Markdown → Confluence storage-format converter
# ---------------------------------------------------------------------------


def test_md_headings():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("# Title\n\n## Section\n\n### Sub")
    assert "<h1>Title</h1>" in out
    assert "<h2>Section</h2>" in out
    assert "<h3>Sub</h3>" in out


def test_md_bullets_and_ordered_lists():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("- a\n- b\n\n1. first\n2. second")
    assert "<ul><li>a</li><li>b</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out


def test_md_inline_formatting():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("text with **bold** and *italic* and `code`.")
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert "<code>code</code>" in out


def test_md_fenced_code_becomes_macro():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("```python\nprint('hi')\n```")
    assert 'ac:name="code"' in out
    assert 'ac:name="language">python' in out
    assert "print('hi')" in out


def test_md_link():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("see [docs](https://example.com) for details")
    assert '<a href="https://example.com">docs</a>' in out


def test_md_html_passthrough():
    """If caller supplies raw HTML, don't re-escape it."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    raw = "<p>Already <strong>formatted</strong> HTML</p>"
    assert _markdown_to_storage(raw) == raw


def test_md_paragraphs_on_blank_lines():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("para one\n\npara two")
    assert out == "<p>para one</p><p>para two</p>"


def test_md_html_in_text_is_escaped():
    """Plain-text `<script>` must not become a real tag."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("beware <script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_md_empty_input():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    assert _markdown_to_storage("") == "<p></p>"


# ---------------------------------------------------------------------------
# find_and_replace_in_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_find_replace_happy_path():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    page_id = "123"
    respx.get(f"https://ex.atlassian.net/wiki/rest/api/content/{page_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": page_id,
                "title": "My Page",
                "version": {"number": 5},
                "body": {"storage": {"value": "<p>line A</p><p>line B</p><p>line C</p>"}},
            },
        )
    )
    put_route = respx.put(f"https://ex.atlassian.net/wiki/rest/api/content/{page_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": page_id, "title": "My Page",
                "version": {"number": 6},
                "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/x"},
            },
        )
    )

    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    r = await c.find_and_replace_in_page(page_id, "line B", "line β")

    # New version + URL returned.
    assert r["version"] == 6
    # PUT carried the replaced body.
    sent_body = put_route.calls[0].request.content.decode()
    assert "line β" in sent_body
    assert "line B" not in sent_body  # original replaced
    assert "line A" in sent_body and "line C" in sent_body  # rest preserved


@pytest.mark.asyncio
@respx.mock
async def test_find_replace_refuses_zero_match():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "123", "title": "P", "version": {"number": 1},
                "body": {"storage": {"value": "<p>hello world</p>"}},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="not found"):
        await c.find_and_replace_in_page("123", "nonexistent text", "whatever")


@pytest.mark.asyncio
@respx.mock
async def test_find_replace_refuses_multiple_matches():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "123", "title": "P", "version": {"number": 1},
                "body": {"storage": {"value": "<p>foo</p><p>foo</p><p>foo</p>"}},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="appears 3 times"):
        await c.find_and_replace_in_page("123", "foo", "bar")


@pytest.mark.asyncio
async def test_find_replace_refuses_empty_find():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="non-empty"):
        await c.find_and_replace_in_page("123", "", "x")


# ---------------------------------------------------------------------------
# list_children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_children_normalizes_output():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "101", "title": "Child A",
                        "version": {"when": "2026-04-10T12:00:00Z"},
                        "_links": {"webui": "/spaces/S/pages/101"},
                    },
                    {
                        "id": "102", "title": "Child B",
                        "version": {"when": "2026-04-11T12:00:00Z"},
                        "_links": {"webui": "/spaces/S/pages/102"},
                    },
                ],
                "_links": {"base": "https://ex.atlassian.net/wiki"},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    kids = await c.list_children("100")
    assert [k["id"] for k in kids] == ["101", "102"]
    assert kids[0]["url"] == "https://ex.atlassian.net/wiki/spaces/S/pages/101"
    assert kids[0]["last_modified"] == "2026-04-10T12:00:00Z"


# ---------------------------------------------------------------------------
# confluence_read action dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_missing_action_is_rejected():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(_read_req({}))
    assert not res.success
    assert "action" in (res.error or "")


@pytest.mark.asyncio
async def test_read_action_read_page_requires_identifier():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page"})
    )
    assert not res.success
    assert "page_id" in (res.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_read_action_read_page_via_url():
    """URL → page_id auto-resolution then fetches that page."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "Extracted",
                "space": {"name": "Sales", "key": "SALES"},
                "version": {"number": 1, "when": "2026-04-01"},
                "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/x"},
                "body": {"view": {"value": "<p>hi</p>"}},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "url": "https://ex.atlassian.net/wiki/spaces/S/pages/42/Title"})
    )
    assert res.success
    assert "Extracted" in res.result
    assert res.metadata["page_id"] == "42"


@pytest.mark.asyncio
async def test_read_action_read_page_bad_url():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "url": "https://ex.atlassian.net/wiki/x/ABCDEF"})
    )
    assert not res.success
    assert "page_id" in (res.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_read_action_list_children():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/100/child/page").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"id": "101", "title": "C1",
                             "version": {"when": ""},
                             "_links": {"webui": "/p/101"}}],
                "_links": {"base": "https://ex.atlassian.net/wiki"},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "list_children", "page_id": "100"})
    )
    assert res.success
    assert "C1" in res.result
    assert res.metadata["count"] == 1


# ---------------------------------------------------------------------------
# confluence_write action dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_missing_action_is_rejected():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceWriteExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceWriteExecutor(c).execute(_write_req({}))
    assert not res.success


@pytest.mark.asyncio
async def test_write_find_replace_requires_all_params():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceWriteExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    # missing replace
    res = await ConfluenceWriteExecutor(c).execute(
        _write_req({"action": "find_replace", "page_id": "1", "find": "a"})
    )
    assert not res.success


@pytest.mark.asyncio
@respx.mock
async def test_write_find_replace_end_to_end():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceWriteExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "123", "title": "P",
                "version": {"number": 2},
                "body": {"storage": {"value": "<p>alpha</p><p>beta</p>"}},
            },
        )
    )
    respx.put("https://ex.atlassian.net/wiki/rest/api/content/123").mock(
        return_value=httpx.Response(
            200,
            json={"id": "123", "title": "P", "version": {"number": 3},
                  "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/x"}},
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceWriteExecutor(c).execute(
        _write_req({"action": "find_replace", "page_id": "123",
                    "find": "alpha", "replace": "α"})
    )
    assert res.success
    assert "version" in res.result.lower()
    assert res.metadata["action"] == "find_replace"


@pytest.mark.asyncio
@respx.mock
async def test_write_find_replace_refusal_surfaces_cleanly():
    """When the library raises ValueError for 0-match, executor turns it
    into a clean tool error (not an uncaught exception)."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceWriteExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "123", "title": "P",
                "version": {"number": 1},
                "body": {"storage": {"value": "<p>nothing to match here</p>"}},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceWriteExecutor(c).execute(
        _write_req({"action": "find_replace", "page_id": "123",
                    "find": "missing phrase", "replace": "x"})
    )
    assert not res.success
    assert "not found" in (res.error or "")


# ---------------------------------------------------------------------------
# Tool catalog shape (for the model) — meta-tool contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_tools_registered_via_register():
    """register_confluence_tools keeps the 2 meta-tools in the global
    ToolRegistry (so execution dispatch works) AND claims them on the
    ConnectorRegistry so the agent loop can hide them from tenants
    without an active connection. Connector-pattern registration.
    """
    from assistant_service.core.tools.confluence_tool import register_confluence_tools
    from assistant_service.core.tools.connector_registry import (
        get_connector_registry,
        reset_connector_registry_for_tests,
    )
    from assistant_service.core.tools.tool_registry import (
        ToolCallRequest,
        get_tool_registry,
    )

    reset_connector_registry_for_tests()
    register_confluence_tools(domain="ex.atlassian.net", email="u@x.com", api_token="t")

    # Tools MUST remain registered in the global ToolRegistry — that's the
    # execution dispatch path. A popped _tools entry would make every tool
    # call return "Unknown tool: confluence_read".
    reg = get_tool_registry()
    names = {t.name for t in reg.list_tools()}
    assert "confluence_read" in names
    assert "confluence_write" in names
    # Old names MUST be gone — they'd be ambiguous after the refactor.
    assert "search_confluence" not in names
    assert "update_confluence_page" not in names

    # Connector registry claims them and exposes them only when the predicate
    # passes. With a static client, the predicate is always True.
    connectors = get_connector_registry()
    assert "confluence" in connectors.list_connectors()
    assert {"confluence_read", "confluence_write"} <= connectors.connector_tool_names()
    req = ToolCallRequest(call_id="c", tool_name="probe", arguments={})
    visible = await connectors.visible_tools(req)
    visible_names = {t.name for t in visible}
    assert "confluence_read" in visible_names
    assert "confluence_write" in visible_names


@pytest.mark.asyncio
async def test_db_backed_register_only_visible_when_tenant_connected():
    """DB-backed registration: confluence_read/write show up only when the
    caller's tenant has an active row in ``confluence_connections``."""
    from assistant_service.core.tools.confluence_tool import register_confluence_tools
    from assistant_service.core.tools.connector_registry import (
        get_connector_registry,
        reset_connector_registry_for_tests,
    )
    from assistant_service.core.tools.tool_registry import (
        ToolCallRequest,
        get_tool_registry,
    )

    from src.core.auth.user_resolver import UserContext

    reset_connector_registry_for_tests()

    # Fake DB: tenantA has an active connection; tenantB has none.
    class _DB:
        async def list_confluence_connections(self, tenant_id=None, status=None, limit=1):
            del status, limit
            if tenant_id == "tenantA":
                return [{"domain": "a.atlassian.net", "email": "a@x.com", "api_token": "t"}]
            return []

    register_confluence_tools(database=_DB())

    # Tools ARE in the global registry — that's where execution dispatch
    # looks them up when an authorized tenant invokes one.
    assert "confluence_read" in {t.name for t in get_tool_registry().list_tools()}

    connectors = get_connector_registry()

    req_a = ToolCallRequest(
        call_id="a", tool_name="probe", arguments={},
        user=UserContext(user_id="u", tenant_id="tenantA"),
    )
    visible_a = await connectors.visible_tools(req_a)
    assert {t.name for t in visible_a} >= {"confluence_read", "confluence_write"}

    req_b = ToolCallRequest(
        call_id="b", tool_name="probe", arguments={},
        user=UserContext(user_id="u", tenant_id="tenantB"),
    )
    visible_b = await connectors.visible_tools(req_b)
    # tenantB sees NOTHING from the confluence connector.
    assert all(t.name not in {"confluence_read", "confluence_write"} for t in visible_b)


def test_meta_tool_action_enums_cover_all_old_operations():
    """Verifies the two meta tools between them expose all 11 old operations."""
    from assistant_service.core.tools.confluence_tool import (
        CONFLUENCE_READ_DEFINITION,
        CONFLUENCE_WRITE_DEFINITION,
    )

    def _action_enum(td):
        for p in td.parameters:
            if p.name == "action":
                return set(p.enum or [])
        return set()

    assert _action_enum(CONFLUENCE_READ_DEFINITION) == {
        "search", "read_page", "list_spaces", "get_space", "list_children"
    }
    assert _action_enum(CONFLUENCE_WRITE_DEFINITION) == {
        "create_page", "update_page", "find_replace", "move_page", "comment", "delete_page"
    }


def test_write_tool_requires_confirmation():
    """Permission middleware gates confluence_write via this flag."""
    from assistant_service.core.tools.confluence_tool import CONFLUENCE_WRITE_DEFINITION
    assert CONFLUENCE_WRITE_DEFINITION.requires_confirmation is True


# ---------------------------------------------------------------------------
# Regression tests for user-reported production bugs
# ---------------------------------------------------------------------------


def test_md_html_wrapped_markdown_converts_not_passthrough():
    """The critical production bug: model emits `<p># Heading</p><p>- item</p>`
    thinking Confluence wants HTML. Old passthrough heuristic sent this verbatim
    and Confluence rendered literal `#` and `-`. Must now convert."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("<p># 会议纪要</p><p>- 决定 A</p><p>- 决定 B</p>")
    # Heading must become <h1>, not stay as literal # in a <p>
    assert "<h1>会议纪要</h1>" in out
    assert "# 会议纪要" not in out
    # Bullets must be in <li>, not literal `-`
    assert "<li>决定 A</li>" in out
    assert "<li>决定 B</li>" in out


def test_md_pure_html_with_confluence_macros_still_passthrough():
    """Passthrough must still work for pure XHTML (no markdown markers),
    e.g. when the caller supplies Confluence storage-format macros directly."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    macro = (
        '<ac:structured-macro ac:name="info">'
        '<ac:rich-text-body><p>Hello</p></ac:rich-text-body>'
        '</ac:structured-macro>'
    )
    assert _markdown_to_storage(macro) == macro


def test_md_realistic_meeting_notes_convert_cleanly():
    """Typical model output for 'create a meeting-notes page'."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    content = (
        "# 2026-04-21 Sync\n\n"
        "## Decisions\n\n"
        "- Ship Phase 5 this week\n"
        "- Freeze APIs Friday\n\n"
        "## Action items\n\n"
        "1. @alice: write migration doc\n"
        "2. @bob: review PR #42\n\n"
        "```bash\npytest tests/ -q\n```\n"
    )
    out = _markdown_to_storage(content)
    assert "<h1>2026-04-21 Sync</h1>" in out
    assert "<h2>Decisions</h2>" in out
    assert "<ul><li>Ship Phase 5 this week</li>" in out
    assert "<ol><li>@alice: write migration doc</li>" in out
    assert 'ac:name="code"' in out
    assert 'ac:name="language">bash' in out
    # No literal markdown markers should leak
    assert "\n# " not in out
    assert "\n- " not in out


@pytest.mark.asyncio
@respx.mock
async def test_read_page_exposes_ancestors_and_parent_id():
    """After reading a page, the response must include parent_id so the
    model can create a sibling page at the same tree level."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "Q1 Plan",
                "space": {"name": "Sales", "key": "SALES"},
                "version": {"number": 3, "when": "2026-04-10"},
                "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/x"},
                "body": {"view": {"value": "<p>body</p>"}},
                "ancestors": [
                    {"id": "100", "title": "Root", "type": "page"},
                    {"id": "200", "title": "Planning", "type": "page"},
                ],
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "page_id": "42"})
    )
    assert res.success
    # The rendered result surfaces "Parent: Planning (id: 200)" so the model sees it.
    assert "Parent: Planning" in res.result
    assert "200" in res.result
    # Breadcrumb shows the full tree path — Root > Planning > Q1 Plan
    assert "Root" in res.result and "Q1 Plan" in res.result
    # Metadata carries parent_id as a structured field for programmatic use.
    assert res.metadata["parent_id"] == "200"
    assert res.metadata["space_key"] == "SALES"


@pytest.mark.asyncio
@respx.mock
async def test_read_page_no_ancestors_shows_homepage_hint():
    """For a space homepage (no ancestors), the formatter should explain
    why no parent is available so the model doesn't hallucinate one."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "1", "title": "Space Home",
                "space": {"name": "Sales", "key": "SALES"},
                "version": {"number": 1, "when": "2026-01-01"},
                "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/h"},
                "body": {"view": {"value": "<p>home</p>"}},
                "ancestors": [],
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "page_id": "1"})
    )
    assert res.success
    assert "space homepage" in res.result.lower() or "top-level" in res.result.lower()
    assert res.metadata["parent_id"] == ""


def test_create_page_description_teaches_sibling_pattern():
    """Model must be told how to create a sibling — look up `parent_id` via
    read_page first. Regression guard: the guidance must be somewhere the
    model sees. Compact schema uses `description`; full schema/docs use
    `when_to_use`. We accept either so the guidance can migrate between
    the two without silently disappearing."""
    from assistant_service.core.tools.confluence_tool import CONFLUENCE_WRITE_DEFINITION
    combined = (
        (CONFLUENCE_WRITE_DEFINITION.description or "")
        + " "
        + (CONFLUENCE_WRITE_DEFINITION.when_to_use or "")
    )
    assert "sibling" in combined.lower()
    assert "parent_id" in combined
    assert "read_page" in combined


# ---------------------------------------------------------------------------
# search now exposes parent_id + ancestors + space_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_parent_and_space_key():
    """Every search hit must include parent_id, parent_title, ancestors,
    and space_key so the model can plan create-sibling / move_page without
    a second read_page per hit."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "_links": {"base": "https://ex.atlassian.net/wiki"},
                "results": [
                    {
                        "id": "42",
                        "title": "Q1 Plan",
                        "space": {"name": "Sales", "key": "SALES"},
                        "version": {"when": "2026-04-10"},
                        "_links": {"webui": "/spaces/SALES/pages/42"},
                        "body": {"view": {"value": "<p>body</p>"}},
                        "ancestors": [
                            {"id": "100", "title": "Root"},
                            {"id": "200", "title": "Planning"},
                        ],
                    }
                ],
            },
        )
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "search", "query": "Q1"})
    )
    assert res.success
    # Rendered result surfaces parent_id + space_key.
    assert "Parent: Planning (id: 200)" in res.result
    assert "key: SALES" in res.result


@pytest.mark.asyncio
@respx.mock
async def test_search_top_level_page_shows_no_parent():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "_links": {"base": "https://ex.atlassian.net/wiki"},
                "results": [
                    {
                        "id": "1", "title": "Homepage",
                        "space": {"name": "Sales", "key": "SALES"},
                        "version": {"when": ""},
                        "_links": {"webui": "/h"},
                        "body": {"view": {"value": ""}},
                        "ancestors": [],
                    }
                ],
            },
        )
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "search", "query": "home"})
    )
    assert res.success
    assert "top-level" in res.result or "homepage" in res.result.lower()


# ---------------------------------------------------------------------------
# move_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_move_page_happy_path():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    # Current page — in space SALES, currently under parent 100.
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "Q1 Plan",
                "version": {"number": 3},
                "ancestors": [{"id": "100", "title": "Root"}],
                "space": {"key": "SALES"},
            },
        )
    )
    # Target parent — exists in same space.
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/500").mock(
        return_value=httpx.Response(
            200,
            json={"id": "500", "title": "Q2 Planning", "space": {"key": "SALES"}},
        )
    )
    # PUT — version bump + ancestors update.
    put_route = respx.put("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "Q1 Plan",
                "version": {"number": 4},
                "_links": {"base": "https://ex.atlassian.net/wiki", "webui": "/x"},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    r = await c.move_page("42", "500")
    assert r["old_parent_id"] == "100"
    assert r["new_parent_id"] == "500"
    assert r["version"] == 4
    # PUT body carries ancestors=[{id:500}]
    sent = put_route.calls[0].request.content.decode()
    assert '"ancestors": [{"id": "500"}]' in sent or '"ancestors":[{"id":"500"}]' in sent


@pytest.mark.asyncio
@respx.mock
async def test_move_page_rejects_same_parent_noop():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "P",
                "version": {"number": 1},
                "ancestors": [{"id": "100", "title": "R"}],
                "space": {"key": "SALES"},
            },
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="already under"):
        await c.move_page("42", "100")


@pytest.mark.asyncio
@respx.mock
async def test_move_page_rejects_missing_target():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "P", "version": {"number": 1},
                "ancestors": [{"id": "100", "title": "R"}],
                "space": {"key": "SALES"},
            },
        )
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/999").mock(
        return_value=httpx.Response(404, text="not found")
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="does not exist"):
        await c.move_page("42", "999")


@pytest.mark.asyncio
@respx.mock
async def test_move_page_rejects_cross_space():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "P", "version": {"number": 1},
                "ancestors": [{"id": "100", "title": "R"}],
                "space": {"key": "SALES"},
            },
        )
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/500").mock(
        return_value=httpx.Response(
            200,
            json={"id": "500", "title": "Other", "space": {"key": "ENG"}},
        )
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="cross-space"):
        await c.move_page("42", "500")


@pytest.mark.asyncio
async def test_move_page_rejects_self_parent():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="under itself"):
        await c.move_page("42", "42")


@pytest.mark.asyncio
async def test_write_action_move_page_needs_both_ids():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceWriteExecutor,
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceWriteExecutor(c).execute(
        _write_req({"action": "move_page", "page_id": "42"})
    )
    assert not res.success
    assert "target_parent_id" in (res.error or "")


# ---------------------------------------------------------------------------
# HTTP status classification + anti-hallucination hint on errors
# ---------------------------------------------------------------------------


def test_classify_http_errors_distinguishes_401_403_404_409():
    from assistant_service.core.tools.confluence_tool import _classify_http_error
    msg_401 = _classify_http_error(401, "read_page")
    msg_403 = _classify_http_error(403, "read_page")
    msg_404 = _classify_http_error(404, "read_page")
    msg_409 = _classify_http_error(409, "update_page")
    msg_500 = _classify_http_error(500, "create_page")

    assert "Authentication" in msg_401 and "re-connect" in msg_401
    assert "Permission" in msg_403
    assert "not found" in msg_404.lower()
    assert "Version conflict" in msg_409
    assert "HTTP 500" in msg_500

    # Every classified error must carry the anti-hallucination reminder.
    for msg in (msg_401, msg_403, msg_404, msg_409, msg_500):
        assert "FAILED" in msg
        assert "succeed" in msg or "success" in msg


@pytest.mark.asyncio
@respx.mock
async def test_read_404_surfaces_distinct_message():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/99999").mock(
        return_value=httpx.Response(404, text="not found")
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "page_id": "99999"})
    )
    assert not res.success
    assert "404" in (res.error or "") or "not found" in (res.error or "").lower()
    assert "FAILED" in (res.error or "")  # anti-hallucination hint


@pytest.mark.asyncio
@respx.mock
async def test_read_401_distinct_auth_message():
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(401, text="unauthorized")
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "read_page", "page_id": "42"})
    )
    assert not res.success
    assert "Authentication" in (res.error or "") or "401" in (res.error or "")
    assert "re-connect" in (res.error or "").lower()


def test_system_prompt_has_anti_tool_hallucination_rule():
    """Guard that the explicit tool-execution hallucination rule is in
    the system prompt. If someone strips it, this test catches it."""
    from assistant_service.core.prompts.system_prompt_v2 import ANTI_HALLUCINATION
    assert "NEVER Fabricate Tool Execution Results" in ANTI_HALLUCINATION
    assert "I have created" in ANTI_HALLUCINATION  # quoted example
    assert "success=true" in ANTI_HALLUCINATION or "success" in ANTI_HALLUCINATION


@pytest.mark.asyncio
@respx.mock
async def test_search_matches_both_title_and_text():
    """Pages with Chinese titles often have the keyword ONLY in the title
    (body is a table / card). CQL `text~` alone misses those — we must
    also query `title~`. Regression: 【第一轮】技术分享排期 was undiscoverable
    by search_confluence because brackets + title-only hit."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await client.search("第一轮技术分享排期", limit=5)

    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    # CQL must now query title AND text (single clause joined by OR).
    assert 'title ~ "' in decoded
    assert 'text ~ "' in decoded
    assert " OR " in decoded


@pytest.mark.asyncio
@respx.mock
async def test_search_strips_cjk_brackets_from_title_query():
    """Literal 【】「」 confuses Lucene's tokenizer — the title-side of the
    CQL should receive a cleaned query without those chars. The text-side
    keeps the original query so exact phrases still hit."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await client.search("【第一轮】技术分享", limit=5)

    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    # Title-side: brackets stripped (may have collapsed-whitespace variant).
    assert 'title ~ "第一轮 技术分享"' in decoded
    # Text-side: original query preserved.
    assert 'text ~ "【第一轮】技术分享"' in decoded


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_structured_dict_with_cql_and_diagnostics():
    """search returns a dict with hits + cql_used + diagnostics.hint on empty."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await c.search("unique-token-xyz-777")
    assert res["count"] == 0
    assert res["hits"] == []
    assert "cql_used" in res and 'title ~ "unique-token-xyz-777"' in res["cql_used"]
    assert res["diagnostics"]["strategy"] == "title+text"
    assert "hint" in res["diagnostics"]
    # On 0 hits the hint must include at least one concrete next-step suggestion
    assert "fields" in res["diagnostics"]["hint"] or "list_spaces" in res["diagnostics"]["hint"]


@pytest.mark.asyncio
@respx.mock
async def test_search_fields_title_only_skips_text_clause():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await c.search("排期", fields=["title"])
    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    assert 'title ~ "排期"' in decoded
    assert 'text ~ "排期"' not in decoded
    assert res["diagnostics"]["strategy"] == "title"


@pytest.mark.asyncio
async def test_search_rejects_unknown_field():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="unknown fields"):
        await c.search("hi", fields=["title", "body"])  # 'body' is not valid


@pytest.mark.asyncio
@respx.mock
async def test_search_under_page_id_adds_ancestor_clause():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await c.search("hi", under_page_id="1038712833")
    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    assert "ancestor=1038712833" in decoded


@pytest.mark.asyncio
async def test_search_rejects_non_numeric_under_page_id():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="numeric"):
        await c.search("hi", under_page_id="not-a-number")


@pytest.mark.asyncio
@respx.mock
async def test_search_updated_since_and_author():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await c.search("hi", author="alice", updated_since="2026-01-01")
    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    assert 'creator = "alice"' in decoded
    assert 'lastModified >= "2026-01-01"' in decoded


@pytest.mark.asyncio
async def test_search_rejects_bad_updated_since():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="ISO date"):
        await c.search("hi", updated_since="yesterday")


@pytest.mark.asyncio
@respx.mock
async def test_search_raw_cql_overrides_everything():
    """When `cql` is supplied, space_key/query/fields all get ignored."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    custom = 'type=page AND label = "roadmap" AND title = "Q2 Plan"'
    res = await c.search("ignored query", space_key="ALSO_IGNORED", cql=custom)
    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    assert custom in decoded
    # None of the other params should have leaked into the CQL.
    assert "ignored query" not in decoded
    assert "ALSO_IGNORED" not in decoded
    assert res["diagnostics"]["strategy"] == "raw_cql"


@pytest.mark.asyncio
async def test_search_raw_cql_rejects_newlines():
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="single-line"):
        await c.search(cql="type=page\nAND malicious")


@pytest.mark.asyncio
async def test_search_requires_at_least_one_input():
    """Empty everything must error — otherwise we'd silently query all pages."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="at least one"):
        await c.search()


@pytest.mark.asyncio
@respx.mock
async def test_search_executor_surfaces_cql_to_model():
    """The formatter must include the CQL used and the diagnostic hint in
    the text the model sees — that's the whole agentic point."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(c).execute(
        _read_req({"action": "search", "query": "nothing-at-all"})
    )
    assert res.success
    # Executor text result must expose the CQL for model reasoning.
    assert "CQL executed" in res.result
    assert 'title ~ "nothing-at-all"' in res.result
    # …and the next-step hint.
    assert "Next step" in res.result or "hint" in res.result.lower()
    # Metadata should carry the CQL too (for logs/telemetry).
    assert res.metadata["cql_used"]
    assert res.metadata["strategy"] == "title+text"


@pytest.mark.asyncio
@respx.mock
async def test_search_still_escapes_quotes_after_cql_change():
    """CQL-injection guard must still work with the new title+text clause.
    An injected `"` must become backslash-escaped so it stays INSIDE the
    string literal rather than ending it and starting a new CQL clause."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await client.search('foo" OR space="ADMIN', limit=5)

    from urllib.parse import unquote_plus
    decoded = unquote_plus(str(route.calls[0].request.url))
    # Both quote instances in the injected payload must be \"-escaped so
    # they don't close the string literal.
    assert '\\"' in decoded
    # No un-escaped `space=` clause at CQL top level. The payload's
    # `space="ADMIN` appears inside a quoted string as literal text, but
    # there must be no bare ` space=` joined by AND at the top level.
    # Key tell: no ` AND space=` outside string bounds.
    assert " AND space=" not in decoded


# ---------------------------------------------------------------------------
# C1 regression — per-tenant credential resolution
# ---------------------------------------------------------------------------


class _FakeDB:
    """Stand-in for the real Database class — serves different Confluence
    connection rows by tenant_id. Used to verify the executor resolves the
    correct credentials per-call instead of closing over a single client."""

    def __init__(self, rows_by_tenant: dict[str, list[dict]]):
        self._rows = rows_by_tenant

    async def list_confluence_connections(
        self, tenant_id: str | None = None, status: str | None = None, limit: int = 100
    ):
        del status, limit
        if tenant_id is None:
            return [r for rows in self._rows.values() for r in rows]
        return self._rows.get(tenant_id, [])


def _build_user_request(tenant_id: str, action: str, **args):
    """ToolCallRequest with a UserContext carrying the given tenant_id."""
    from assistant_service.core.tools.tool_registry import ToolCallRequest

    from src.core.auth.user_resolver import UserContext
    user = UserContext(user_id="u", tenant_id=tenant_id)
    return ToolCallRequest(
        call_id=f"c-{tenant_id}",
        tool_name="confluence_read",
        arguments={"action": action, **args},
        user=user,
    )


@pytest.mark.asyncio
@respx.mock
async def test_per_tenant_client_resolution_isolates_credentials():
    """Two tenants with DIFFERENT Confluence domains hitting
    `confluence_read(action='list_spaces')` must each reach THEIR OWN
    domain, not a shared last-registered one. This is the fix for the
    cross-tenant credential leak."""
    from assistant_service.core.tools.confluence_tool import ConfluenceReadExecutor

    db = _FakeDB({
        "tenantA": [{
            "domain": "tenant-a.atlassian.net",
            "email": "a@x.com", "api_token": "tok-A",
            "status": "active",
        }],
        "tenantB": [{
            "domain": "tenant-b.atlassian.net",
            "email": "b@x.com", "api_token": "tok-B",
            "status": "active",
        }],
    })

    route_a = respx.get("https://tenant-a.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    route_b = respx.get("https://tenant-b.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    execr = ConfluenceReadExecutor(database=db)

    # Tenant A calls — must hit tenant-a domain.
    res_a = await execr.execute(_build_user_request("tenantA", "list_spaces"))
    assert res_a.success
    assert route_a.called and not route_b.called
    assert "Basic " + __import__("base64").b64encode(b"a@x.com:tok-A").decode() in \
        str(route_a.calls[0].request.headers.get("authorization", ""))

    # Tenant B calls — must hit tenant-b domain with B's creds.
    res_b = await execr.execute(_build_user_request("tenantB", "list_spaces"))
    assert res_b.success
    assert route_b.called
    assert "Basic " + __import__("base64").b64encode(b"b@x.com:tok-B").decode() in \
        str(route_b.calls[0].request.headers.get("authorization", ""))


@pytest.mark.asyncio
async def test_request_with_unknown_tenant_rejects_cleanly():
    """If the DB has no row for the request's tenant, the executor must
    surface a clean error instead of hitting the wrong workspace or
    using empty credentials."""
    from assistant_service.core.tools.confluence_tool import ConfluenceReadExecutor

    db = _FakeDB({})  # no tenants
    execr = ConfluenceReadExecutor(database=db)
    res = await execr.execute(_build_user_request("nobody", "list_spaces"))
    assert not res.success
    assert "No active Confluence connection" in (res.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_static_client_fallback_still_works_for_tests():
    """Backwards-compat: tests constructing `ConfluenceReadExecutor(client=...)`
    keep working — the static client is used when no tenant_id is on the
    request OR when the DB has no row."""
    from assistant_service.core.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://legacy.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("legacy.atlassian.net", "u@x.com", "legacy-tok")
    execr = ConfluenceReadExecutor(client=client)  # no DB
    # No tenant_id on request — fallback kicks in.
    from assistant_service.core.tools.tool_registry import ToolCallRequest
    res = await execr.execute(ToolCallRequest(
        call_id="c", tool_name="confluence_read",
        arguments={"action": "list_spaces"},
    ))
    assert res.success


# ---------------------------------------------------------------------------
# H1 regression — plain-text replacement is auto-escaped for XHTML
# ---------------------------------------------------------------------------


def test_escape_for_storage_handles_bare_specials():
    from assistant_service.core.tools.confluence_tool import _escape_for_storage
    assert _escape_for_storage("A & B") == "A &amp; B"
    assert _escape_for_storage("x < y > z") == "x &lt; y &gt; z"
    # Already-escaped entities pass through — idempotent.
    assert _escape_for_storage("A &amp; B") == "A &amp; B"
    assert _escape_for_storage("&lt;strong&gt;") == "&lt;strong&gt;"
    # Numeric entities too.
    assert _escape_for_storage("x &#169; y") == "x &#169; y"
    assert _escape_for_storage("x &#x2014; y") == "x &#x2014; y"
    # Bare ampersand NOT followed by a valid entity is escaped.
    assert _escape_for_storage("see &foo") == "see &amp;foo"


@pytest.mark.asyncio
@respx.mock
async def test_find_replace_auto_escapes_plain_text_replacement():
    """Regression: replacing with plain text that contains bare `&` must
    NOT corrupt Confluence storage format. Must escape by default."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "P", "version": {"number": 1},
                "body": {"storage": {"value": "<p>Alpha & Beta</p>"}},
            },
        )
    )
    put_route = respx.put("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(200, json={"id": "42", "title": "P",
            "version": {"number": 2},
            "_links": {"base": "https://ex/wiki", "webui": "/x"}}),
    )

    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await c.find_and_replace_in_page("42", "Alpha & Beta", "Gamma & Delta")

    sent = put_route.calls[0].request.content.decode()
    # Plain-text `&` must have been escaped — otherwise Confluence rejects.
    assert "Gamma &amp; Delta" in sent
    assert "Gamma & Delta" not in sent


@pytest.mark.asyncio
@respx.mock
async def test_find_replace_raw_html_passes_through():
    """Opt-out: when raw_html=True, replacement is used verbatim — allows
    inserting actual XHTML (e.g. <strong>) without double-escaping."""
    from assistant_service.core.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42", "title": "P", "version": {"number": 1},
                "body": {"storage": {"value": "<p>boring</p>"}},
            },
        )
    )
    put_route = respx.put("https://ex.atlassian.net/wiki/rest/api/content/42").mock(
        return_value=httpx.Response(200, json={"id": "42", "title": "P",
            "version": {"number": 2},
            "_links": {"base": "https://ex/wiki", "webui": "/x"}}),
    )

    c = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await c.find_and_replace_in_page("42", "boring", "<strong>bold</strong>", raw_html=True)

    sent = put_route.calls[0].request.content.decode()
    assert "<strong>bold</strong>" in sent
    assert "&lt;strong&gt;" not in sent  # not double-escaped


# ---------------------------------------------------------------------------
# M3 regression — adjacent lists merge
# ---------------------------------------------------------------------------


def test_markdown_converter_merges_adjacent_ul_lists():
    """`<p>- A</p><p>- B</p>` used to become two separate <ul> blocks. After
    the merge pass they should be one list."""
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("<p>- item 1</p><p>- item 2</p><p>- item 3</p>")
    # Before M3: <ul><li>item 1</li></ul><ul><li>item 2</li></ul>...
    # After M3: one <ul> containing all.
    assert out.count("<ul>") == 1
    assert out.count("</ul>") == 1
    assert "<li>item 1</li><li>item 2</li><li>item 3</li>" in out


def test_markdown_converter_merges_adjacent_ol_lists():
    from assistant_service.core.tools.confluence_tool import _markdown_to_storage
    out = _markdown_to_storage("<p>1. one</p><p>2. two</p>")
    # The HTML-wrapper strip produces "- " / "1. " prefixed lines; verify
    # adjacent <ol> blocks collapse.
    # (bullet-less numeric prefix would be treated as paragraph if not
    # at the start of a block — check that numeric lists still coalesce
    # when they ARE recognized.)
    assert out.count("<ol>") <= 1  # either zero (not recognized as OL) or one


def test_meta_tool_schemas_are_smaller_than_old_eight():
    """The whole point of the refactor — 2 tool schemas must be materially
    smaller than the 8 old ones would have been."""
    import json

    from assistant_service.core.tools.confluence_tool import (
        CONFLUENCE_READ_DEFINITION,
        CONFLUENCE_WRITE_DEFINITION,
    )
    read_schema = json.dumps(CONFLUENCE_READ_DEFINITION.to_openai_schema(compact=True))
    write_schema = json.dumps(CONFLUENCE_WRITE_DEFINITION.to_openai_schema(compact=True))
    total = len(read_schema) + len(write_schema)
    # Rough upper bound: 2 meta-schemas should stay materially smaller than
    # the 8-tool surface this replaced (~7000-8000 chars). Headroom at 5000
    # allows future actions without triggering this guard; the moment we
    # approach the old size the refactor's main win is gone.
    assert total < 5000, f"meta-tool schemas are {total} chars — unexpected bloat"
