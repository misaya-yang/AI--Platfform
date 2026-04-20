"""
Tests for Confluence space tools (list_confluence_spaces, get_confluence_space).

Covers:
- V2 API parameter normalization (comma-string vs list)
- Client-side fuzzy matching on name/key/description
- Sensible output when zero spaces match
- Error path handling (HTTP errors surface as clean tool errors, not tracebacks)

Uses respx for httpx mocking.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx


# ---------------------------------------------------------------------------
# Pure-function tests (no HTTP)
# ---------------------------------------------------------------------------


def test_filter_and_rank_prioritizes_name_match():
    from src.services.assistant.tools.confluence_tool import _filter_and_rank_spaces

    spaces = [
        {"name": "Engineering", "key": "ENG", "description": ""},
        {"name": "Sales Team", "key": "SALES", "description": "Revenue and GTM"},
        {"name": "HR", "key": "HR", "description": "Talks about sales enablement sometimes"},
    ]
    ranked = _filter_and_rank_spaces(spaces, "sales")
    # Name match beats description match.
    assert ranked[0]["name"] == "Sales Team"
    # HR mentions sales in description so it ranks below but is included.
    assert any(s["name"] == "HR" for s in ranked)
    # Engineering has no match → excluded.
    assert not any(s["name"] == "Engineering" for s in ranked)


def test_filter_and_rank_empty_query_passes_through():
    from src.services.assistant.tools.confluence_tool import _filter_and_rank_spaces

    spaces = [{"name": "A", "key": "A", "description": ""}]
    assert _filter_and_rank_spaces(spaces, "") == spaces


def test_filter_and_rank_multi_token_query():
    """A query like 'sales qa' should match spaces that hit EITHER token."""
    from src.services.assistant.tools.confluence_tool import _filter_and_rank_spaces

    spaces = [
        {"name": "Sales QA", "key": "SALESQA", "description": ""},
        {"name": "Sales", "key": "SALES", "description": ""},
        {"name": "QA", "key": "QA", "description": ""},
        {"name": "Engineering", "key": "ENG", "description": ""},
    ]
    ranked = _filter_and_rank_spaces(spaces, "sales qa")
    # The combined match ranks highest.
    assert ranked[0]["name"] == "Sales QA"
    assert not any(s["name"] == "Engineering" for s in ranked)


# ---------------------------------------------------------------------------
# Client-level (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_list_spaces_normalizes_v2_response():
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "123",
                        "key": "SALES",
                        "name": "Sales Team",
                        "type": "global",
                        "status": "current",
                        "description": {"plain": {"value": "All things sales."}},
                        "homepageId": "999",
                        "_links": {"webui": "/spaces/SALES"},
                    }
                ],
                "_links": {},
            },
        )
    )

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    spaces = await client.list_spaces()
    assert len(spaces) == 1
    s = spaces[0]
    assert s["key"] == "SALES"
    assert s["name"] == "Sales Team"
    assert s["description"] == "All things sales."
    assert s["url"].endswith("/wiki/spaces/SALES")
    assert s["homepage_id"] == "999"


@pytest.mark.asyncio
@respx.mock
async def test_list_spaces_applies_query_filter_client_side():
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "1", "key": "ENG", "name": "Engineering", "type": "global",
                     "status": "current", "description": {"plain": {"value": ""}}},
                    {"id": "2", "key": "SALES", "name": "Sales", "type": "global",
                     "status": "current", "description": {"plain": {"value": ""}}},
                    {"id": "3", "key": "HR", "name": "HR", "type": "global",
                     "status": "current", "description": {"plain": {"value": ""}}},
                ]
            },
        )
    )

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    spaces = await client.list_spaces(query="sales")
    assert len(spaces) == 1
    assert spaces[0]["key"] == "SALES"


@pytest.mark.asyncio
@respx.mock
async def test_get_space_by_id_v2():
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "key": "XYZ",
                "name": "Project X",
                "type": "global",
                "status": "current",
                "description": "Plain string desc",
                "_links": {"webui": "/spaces/XYZ"},
            },
        )
    )

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    sp = await client.get_space(space_id="42")
    assert sp is not None
    assert sp["key"] == "XYZ"
    assert sp["description"] == "Plain string desc"


@pytest.mark.asyncio
@respx.mock
async def test_get_space_returns_none_on_404():
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces/999").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    sp = await client.get_space(space_id="999")
    assert sp is None


@pytest.mark.asyncio
@respx.mock
async def test_get_space_by_key_uses_list_endpoint():
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    # get_space(space_key=...) should hit the LIST endpoint with keys=...,
    # not the item endpoint (which needs numeric id).
    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "7",
                        "key": "SALES",
                        "name": "Sales",
                        "type": "global",
                        "status": "current",
                        "description": "",
                        "_links": {"webui": "/spaces/SALES"},
                    }
                ]
            },
        )
    )

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    sp = await client.get_space(space_key="SALES")
    assert sp is not None
    assert sp["key"] == "SALES"
    assert sp["id"] == "7"


# ---------------------------------------------------------------------------
# Executor-level
# ---------------------------------------------------------------------------


def _read_req(arguments: dict) -> "ToolCallRequest":  # type: ignore[name-defined]
    from src.services.assistant.tools.tool_registry import ToolCallRequest
    return ToolCallRequest(
        call_id="c", tool_name="confluence_read", arguments=arguments
    )


def _write_req(arguments: dict) -> "ToolCallRequest":  # type: ignore[name-defined]
    from src.services.assistant.tools.tool_registry import ToolCallRequest
    return ToolCallRequest(
        call_id="c", tool_name="confluence_write", arguments=arguments
    )


@pytest.mark.asyncio
@respx.mock
async def test_read_list_spaces_accepts_comma_string_keys():
    """Model often serializes arrays as comma-strings; executor must cope."""
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )

    route = respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "list_spaces", "keys": "ENG,SALES"})
    )
    assert res.success
    sent_url = str(route.calls[0].request.url)
    assert "keys=ENG%2CSALES" in sent_url or "keys=ENG,SALES" in sent_url


@pytest.mark.asyncio
@respx.mock
async def test_read_list_spaces_empty_gives_helpful_message():
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "list_spaces", "query": "nonexistent"})
    )
    assert res.success  # empty ≠ failure
    assert "No Confluence spaces matched" in res.result


@pytest.mark.asyncio
@respx.mock
async def test_read_list_spaces_caps_shown_at_25():
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    big = {
        "results": [
            {
                "id": str(i), "key": f"S{i}", "name": f"Space {i}",
                "type": "global", "status": "current", "description": "",
                "_links": {"webui": f"/spaces/S{i}"},
            }
            for i in range(60)
        ]
    }
    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(200, json=big)
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "list_spaces"})
    )
    assert res.success
    assert "and 35 more" in res.result
    assert res.metadata["count"] == 60
    assert res.metadata["shown"] == 25


@pytest.mark.asyncio
async def test_read_get_space_requires_id_or_key():
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "get_space"})
    )
    assert not res.success
    assert "space_id" in (res.error or "") and "space_key" in (res.error or "")


@pytest.mark.asyncio
async def test_read_rejects_unknown_action():
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "bogus"})
    )
    assert not res.success
    assert "Unknown action" in (res.error or "")


@pytest.mark.asyncio
@respx.mock
async def test_search_escapes_cql_injection_attempts():
    """A query with a quote must be escaped so it can't inject additional
    CQL clauses (e.g. `foo" OR space="ADMIN`)."""
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    route = respx.get("https://ex.atlassian.net/wiki/rest/api/content/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    await client.search('foo" OR space="ADMIN', limit=5)

    sent_url = str(route.calls[0].request.url)
    # The injected quote must be backslash-escaped in the CQL, not closing the literal.
    assert 'space%3D%22ADMIN' not in sent_url  # no extra space=ADMIN clause
    # Sanity: escaped sequence made it in
    assert "%5C%22" in sent_url or "\\%22" in sent_url or 'foo\\"' in sent_url


@pytest.mark.asyncio
async def test_search_rejects_bad_space_key():
    """A space_key must match `[A-Za-z0-9]+` — anything else is a CQL injection attempt."""
    from src.services.assistant.tools.confluence_tool import ConfluenceAPIClient

    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    with pytest.raises(ValueError, match="invalid space_key"):
        await client.search("foo", space_key='ADMIN" OR text~"leak')


@pytest.mark.asyncio
@respx.mock
async def test_read_http_error_clean_surface():
    """A 500 from the server should become a clean tool error — not a traceback."""
    from src.services.assistant.tools.confluence_tool import (
        ConfluenceAPIClient,
        ConfluenceReadExecutor,
    )
    respx.get("https://ex.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    client = ConfluenceAPIClient("ex.atlassian.net", "u@x.com", "tok")
    res = await ConfluenceReadExecutor(client).execute(
        _read_req({"action": "list_spaces"})
    )
    assert not res.success
    assert "HTTP 500" in (res.error or "")
