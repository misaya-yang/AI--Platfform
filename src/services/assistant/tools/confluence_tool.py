"""
Confluence Tool — AI can search and read Confluence pages during chat.

Uses the stored Confluence connection credentials (API Token + Email)
to call Atlassian REST API directly. Registered as built-in tools
so the LLM can invoke them like any other tool.

Tools:
  - search_confluence: Search pages via CQL full-text search
  - read_confluence_page: Read a specific page's content by ID or title
"""

from __future__ import annotations

import time
from base64 import b64encode
from typing import Any

import httpx

from ....core.observability.logging import get_logger
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExample,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

logger = get_logger(__name__)


# ─── Tool Definitions ────────────────────────────────────────────────

SEARCH_CONFLUENCE_DEFINITION = ToolDefinition(
    name="search_confluence",
    description=(
        "Search Confluence pages by keyword. Returns page titles, excerpts, "
        "and links. Use this when the user asks about company wiki, documentation, "
        "internal knowledge, or mentions Confluence."
    ),
    parameters=[
        ToolParameter(
            name="query",
            type="string",
            description="Search query (keywords or phrase). Supports CQL text search.",
            required=True,
        ),
        ToolParameter(
            name="space_key",
            type="string",
            description="Confluence space key to limit search (e.g., 'DEV', 'HR'). Optional — searches all spaces if omitted.",
            required=False,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max results to return (1-20). Default 5.",
            required=False,
            default=5,
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use=(
        "Use when the user asks about internal documentation, company wiki pages, "
        "project plans, meeting notes, technical specs, or any content that would "
        "be stored in Confluence. Also use when user explicitly mentions Confluence."
    ),
    when_not_to_use=(
        "Do not use for general knowledge questions, external information, "
        "or when the user is clearly asking about something not in Confluence."
    ),
    examples=[
        ToolExample(
            description="Search for onboarding docs",
            input={"query": "new employee onboarding process"},
            expected_output="Returns Confluence pages about onboarding procedures",
        ),
        ToolExample(
            description="Search in specific space",
            input={"query": "API authentication", "space_key": "DEV", "limit": 3},
            expected_output="Returns dev documentation about API auth",
        ),
    ],
    timeout_seconds=15,
)

READ_CONFLUENCE_PAGE_DEFINITION = ToolDefinition(
    name="read_confluence_page",
    description=(
        "Read the full content of a specific Confluence page. "
        "Use after search_confluence to get detailed page content, "
        "or when user provides a page ID or exact title."
    ),
    parameters=[
        ToolParameter(
            name="page_id",
            type="string",
            description="Confluence page ID (numeric). Get this from search_confluence results.",
            required=False,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Exact page title to look up. Used when page_id is not available.",
            required=False,
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use to read the full content of a Confluence page after finding it via search.",
    when_not_to_use="Do not use without first searching — you need either a page_id or exact title.",
    examples=[
        ToolExample(
            description="Read page by ID",
            input={"page_id": "12345678"},
            expected_output="Returns the full text content of the page",
        ),
    ],
    timeout_seconds=15,
)


# ─── Confluence API Client ───────────────────────────────────────────

class ConfluenceAPIClient:
    """Lightweight Confluence REST API client using stored credentials."""

    def __init__(self, domain: str, email: str, api_token: str):
        self.base_url = f"https://{domain}/wiki/rest/api"
        self.auth_header = "Basic " + b64encode(f"{email}:{api_token}".encode()).decode()

    async def search(self, query: str, space_key: str | None = None, limit: int = 5) -> list[dict]:
        """Search pages via CQL."""
        cql_parts = [f'type=page AND text~"{query}"']
        if space_key:
            cql_parts.insert(0, f"space={space_key}")
        cql = " AND ".join(cql_parts)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/content/search",
                params={"cql": cql, "limit": limit, "expand": "body.view,space,version"},
                headers={"Authorization": self.auth_header},
            )
            resp.raise_for_status()

        data = resp.json()
        results = []
        for page in data.get("results", []):
            body_html = page.get("body", {}).get("view", {}).get("value", "")
            # Strip HTML
            import re
            text = re.sub(r"<[^>]+>", " ", body_html)
            text = re.sub(r"\s+", " ", text).strip()[:800]

            base_url = data.get("_links", {}).get("base", f"https://{self.base_url.split('/wiki')[0].split('//')[1]}")
            web_link = page.get("_links", {}).get("webui", "")

            results.append({
                "id": page["id"],
                "title": page["title"],
                "space": page.get("space", {}).get("name", ""),
                "space_key": page.get("space", {}).get("key", ""),
                "url": f"{base_url}{web_link}" if web_link else "",
                "excerpt": text[:300],
                "last_modified": page.get("version", {}).get("when", ""),
            })
        return results

    async def read_page(self, page_id: str | None = None, title: str | None = None) -> dict | None:
        """Read a single page by ID or title."""
        async with httpx.AsyncClient(timeout=15) as client:
            if page_id:
                resp = await client.get(
                    f"{self.base_url}/content/{page_id}",
                    params={"expand": "body.view,space,version"},
                    headers={"Authorization": self.auth_header},
                )
            elif title:
                resp = await client.get(
                    f"{self.base_url}/content",
                    params={"title": title, "expand": "body.view,space,version", "limit": 1},
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
        import re
        body_html = page.get("body", {}).get("view", {}).get("value", "")
        text = re.sub(r"<[^>]+>", " ", body_html)
        text = re.sub(r"\s+", " ", text).strip()

        return {
            "id": page["id"],
            "title": page["title"],
            "space": page.get("space", {}).get("name", ""),
            "content": text[:5000],  # Limit to 5K chars for context window
            "url": page.get("_links", {}).get("base", "") + page.get("_links", {}).get("webui", ""),
            "last_modified": page.get("version", {}).get("when", ""),
            "version": page.get("version", {}).get("number", 0),
        }


# ─── Tool Executors ──────────────────────────────────────────────────

class ConfluenceSearchExecutor(ToolExecutor):
    """Executor for search_confluence tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        query = request.arguments.get("query", "")
        space_key = request.arguments.get("space_key")
        limit = min(int(request.arguments.get("limit", 5)), 20)

        try:
            results = await self.client.search(query, space_key, limit)
            duration = (time.time() - start) * 1000

            if not results:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=f"No Confluence pages found for: {query}",
                    duration_ms=duration,
                )

            formatted = []
            for r in results:
                formatted.append(
                    f"**{r['title']}** (Space: {r['space']})\n"
                    f"ID: {r['id']} | URL: {r['url']}\n"
                    f"{r['excerpt']}\n"
                )

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"Found {len(results)} Confluence pages:\n\n" + "\n---\n".join(formatted),
                duration_ms=duration,
                metadata={"count": len(results)},
            )
        except Exception as e:
            logger.error(f"Confluence search failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                result=None,
                error=f"Confluence search failed: {e}",
                duration_ms=(time.time() - start) * 1000,
            )


class ConfluenceReadExecutor(ToolExecutor):
    """Executor for read_confluence_page tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        page_id = request.arguments.get("page_id")
        title = request.arguments.get("title")

        if not page_id and not title:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                result=None,
                error="Either page_id or title is required.",
            )

        try:
            page = await self.client.read_page(page_id=page_id, title=title)
            duration = (time.time() - start) * 1000
            if not page:
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=True,
                    result=f"Page not found: {page_id or title}",
                    duration_ms=duration,
                )

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=(
                    f"# {page['title']}\n"
                    f"Space: {page['space']} | Version: {page['version']} | "
                    f"Modified: {page['last_modified']}\n"
                    f"URL: {page['url']}\n\n"
                    f"{page['content']}"
                ),
                duration_ms=duration,
                metadata={"page_id": page["id"]},
            )
        except Exception as e:
            logger.error(f"Confluence read failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                result=None,
                error=f"Confluence read failed: {e}",
                duration_ms=(time.time() - start) * 1000,
            )


# ─── Registration ─────────────────────────────────────────────────────

def register_confluence_tools(
    domain: str = "",
    email: str = "",
    api_token: str = "",
    tenant_id: str = "",
    database: Any = None,
) -> None:
    """Register Confluence tools with direct credentials or database lookup."""
    if domain and email and api_token:
        client = ConfluenceAPIClient(domain, email, api_token)
    else:
        logger.warning("Confluence tools registered without credentials — will fail on use")
        client = ConfluenceAPIClient("", "", "")

    register_tool(SEARCH_CONFLUENCE_DEFINITION, ConfluenceSearchExecutor(client))
    register_tool(READ_CONFLUENCE_PAGE_DEFINITION, ConfluenceReadExecutor(client))
    logger.info(f"Registered Confluence tools for {domain or 'unconfigured'}")
