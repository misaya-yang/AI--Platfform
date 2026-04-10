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


# ─── Write tool definitions ─────────────────────────────────────────

CREATE_CONFLUENCE_PAGE_DEFINITION = ToolDefinition(
    name="create_confluence_page",
    description=(
        "Create a new Confluence page in the specified space. Returns the new page ID and URL. "
        "Use this when the user explicitly asks to create, draft, or add a new page to Confluence. "
        "Content is automatically converted from plain text/markdown to Confluence storage format."
    ),
    parameters=[
        ToolParameter(
            name="space_key",
            type="string",
            description="Confluence space key where the page will be created (e.g., 'DEV', 'HR', 'TEAM'). Required.",
            required=True,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Title of the new page. Must be unique within the space.",
            required=True,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="Page content as plain text or markdown. Paragraphs separated by blank lines. HTML accepted for rich formatting.",
            required=True,
        ),
        ToolParameter(
            name="parent_id",
            type="string",
            description="Optional parent page ID to nest this page under. Omit for top-level.",
            required=False,
        ),
    ],
    category=ToolCategory.INTEGRATION,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=True,
    when_to_use=(
        "Use when the user explicitly asks to create, draft, publish, or add a new Confluence page. "
        "Always confirm the space_key and title with the user if ambiguous."
    ),
    when_not_to_use=(
        "Do not use for editing existing pages (use update_confluence_page). "
        "Do not use without explicit user intent to write — never create pages speculatively."
    ),
    examples=[
        ToolExample(
            description="Create a new team meeting notes page",
            input={"space_key": "TEAM", "title": "Weekly Sync 2026-04-10", "content": "# Agenda\n\n- Project updates\n- Blockers"},
            expected_output="Returns new page ID and URL",
        ),
    ],
    timeout_seconds=20,
)

UPDATE_CONFLUENCE_PAGE_DEFINITION = ToolDefinition(
    name="update_confluence_page",
    description=(
        "Update (overwrite) the content of an existing Confluence page. "
        "Fetches the current version automatically and increments it. "
        "The provided content REPLACES the entire page body — read the page first if you want to append."
    ),
    parameters=[
        ToolParameter(
            name="page_id",
            type="string",
            description="Confluence page ID (numeric) of the page to update.",
            required=True,
        ),
        ToolParameter(
            name="content",
            type="string",
            description="New page body content as plain text or markdown. REPLACES existing content entirely.",
            required=True,
        ),
        ToolParameter(
            name="title",
            type="string",
            description="Optional new title. If omitted, keeps the existing title.",
            required=False,
        ),
    ],
    category=ToolCategory.INTEGRATION,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=True,
    when_to_use=(
        "Use when the user explicitly asks to update, edit, or rewrite a Confluence page. "
        "Typically preceded by read_confluence_page to get the existing content."
    ),
    when_not_to_use=(
        "Do not use for creating new pages (use create_confluence_page). "
        "Do not use without the user's explicit update intent — page_id alone is not enough."
    ),
    examples=[
        ToolExample(
            description="Update a meeting notes page with decisions",
            input={"page_id": "12345678", "content": "Decisions: ..."},
            expected_output="Returns updated version number",
        ),
    ],
    timeout_seconds=20,
)

ADD_CONFLUENCE_COMMENT_DEFINITION = ToolDefinition(
    name="add_confluence_comment",
    description=(
        "Add a comment to a Confluence page. Lightweight, reversible write. "
        "Use this to leave feedback, suggestions, or notes on a page."
    ),
    parameters=[
        ToolParameter(
            name="page_id",
            type="string",
            description="Confluence page ID to comment on.",
            required=True,
        ),
        ToolParameter(
            name="comment",
            type="string",
            description="Comment text. Plain text or simple markdown.",
            required=True,
        ),
    ],
    category=ToolCategory.INTEGRATION,
    risk_level=ToolRiskLevel.LOW,
    when_to_use="Use when the user asks to comment on, leave feedback on, or annotate a Confluence page.",
    when_not_to_use="Do not use for page edits (use update_confluence_page) — comments are separate entities.",
    examples=[
        ToolExample(
            description="Leave feedback on a design doc",
            input={"page_id": "12345678", "comment": "LGTM, one nit: consider adding a rollback plan."},
            expected_output="Returns new comment ID",
        ),
    ],
    timeout_seconds=15,
)

DELETE_CONFLUENCE_PAGE_DEFINITION = ToolDefinition(
    name="delete_confluence_page",
    description=(
        "Delete a Confluence page (moves it to trash — recoverable by admin). "
        "This is destructive and requires explicit user confirmation."
    ),
    parameters=[
        ToolParameter(
            name="page_id",
            type="string",
            description="Confluence page ID to delete.",
            required=True,
        ),
    ],
    category=ToolCategory.INTEGRATION,
    risk_level=ToolRiskLevel.MEDIUM,
    requires_confirmation=True,
    when_to_use=(
        "Use ONLY when the user explicitly asks to delete, remove, or archive a specific Confluence page "
        "AND has confirmed the page ID. Always confirm with the user before executing."
    ),
    when_not_to_use=(
        "Never use speculatively. Never delete pages the user did not explicitly name. "
        "If unsure, ask the user to confirm the page ID and title first."
    ),
    examples=[
        ToolExample(
            description="Delete an obsolete draft",
            input={"page_id": "12345678"},
            expected_output="Page moved to trash",
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

    @staticmethod
    def _to_storage_html(content: str) -> str:
        """Convert plain text / simple markdown to Confluence storage format.

        Minimal conversion: wraps lines in <p>, escapes HTML, preserves blank-line paragraphs.
        Users can pass raw HTML if they want rich formatting.
        """
        import html as html_mod
        # If it looks like HTML already, trust the caller
        stripped = content.strip()
        if stripped.startswith("<") and stripped.endswith(">"):
            return content
        # Otherwise escape + wrap paragraphs
        paragraphs = content.split("\n\n")
        escaped = [f"<p>{html_mod.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs if p.strip()]
        return "".join(escaped) if escaped else "<p></p>"


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


class ConfluenceCreatePageExecutor(ToolExecutor):
    """Executor for create_confluence_page tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        space_key = request.arguments.get("space_key", "")
        title = request.arguments.get("title", "")
        content = request.arguments.get("content", "")
        parent_id = request.arguments.get("parent_id")

        if not space_key or not title or not content:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="space_key, title, and content are all required.",
            )

        try:
            page = await self.client.create_page(space_key, title, content, parent_id)
            duration = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=(
                    f"Created Confluence page:\n"
                    f"**{page['title']}** (ID: {page['id']}, v{page['version']})\n"
                    f"URL: {page['url']}"
                ),
                duration_ms=duration,
                metadata={"page_id": page["id"], "version": page["version"]},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Confluence create failed: {e.response.status_code} {e.response.text[:200]}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Create failed ({e.response.status_code}): {e.response.text[:200]}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            logger.error(f"Confluence create failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Confluence create failed: {e}",
                duration_ms=(time.time() - start) * 1000,
            )


class ConfluenceUpdatePageExecutor(ToolExecutor):
    """Executor for update_confluence_page tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        page_id = request.arguments.get("page_id", "")
        content = request.arguments.get("content", "")
        title = request.arguments.get("title")

        if not page_id or not content:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="page_id and content are required.",
            )

        try:
            page = await self.client.update_page(page_id, content, title)
            duration = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=(
                    f"Updated Confluence page:\n"
                    f"**{page['title']}** (ID: {page['id']}, v{page['version']})\n"
                    f"URL: {page['url']}"
                ),
                duration_ms=duration,
                metadata={"page_id": page["id"], "version": page["version"]},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Confluence update failed: {e.response.status_code} {e.response.text[:200]}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Update failed ({e.response.status_code}): {e.response.text[:200]}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            logger.error(f"Confluence update failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Confluence update failed: {e}",
                duration_ms=(time.time() - start) * 1000,
            )


class ConfluenceAddCommentExecutor(ToolExecutor):
    """Executor for add_confluence_comment tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        page_id = request.arguments.get("page_id", "")
        comment = request.arguments.get("comment", "")

        if not page_id or not comment:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="page_id and comment are required.",
            )

        try:
            result = await self.client.add_comment(page_id, comment)
            duration = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"Added comment (ID: {result['id']}) to page {page_id}",
                duration_ms=duration,
                metadata={"comment_id": result["id"], "page_id": page_id},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Confluence comment failed: {e.response.status_code} {e.response.text[:200]}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Comment failed ({e.response.status_code}): {e.response.text[:200]}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            logger.error(f"Confluence comment failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Confluence comment failed: {e}",
                duration_ms=(time.time() - start) * 1000,
            )


class ConfluenceDeletePageExecutor(ToolExecutor):
    """Executor for delete_confluence_page tool."""

    def __init__(self, client: ConfluenceAPIClient):
        self.client = client

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        start = time.time()
        page_id = request.arguments.get("page_id", "")

        if not page_id:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error="page_id is required.",
            )

        try:
            result = await self.client.delete_page(page_id)
            duration = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                result=f"Deleted (trashed) Confluence page {page_id}. Recoverable from trash by admin.",
                duration_ms=duration,
                metadata={"page_id": page_id, "status": result["status"]},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"Confluence delete failed: {e.response.status_code} {e.response.text[:200]}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Delete failed ({e.response.status_code}): {e.response.text[:200]}",
                duration_ms=(time.time() - start) * 1000,
            )
        except Exception as e:
            logger.error(f"Confluence delete failed: {e}")
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=f"Confluence delete failed: {e}",
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

    # Read tools
    register_tool(SEARCH_CONFLUENCE_DEFINITION, ConfluenceSearchExecutor(client))
    register_tool(READ_CONFLUENCE_PAGE_DEFINITION, ConfluenceReadExecutor(client))
    # Write tools
    register_tool(CREATE_CONFLUENCE_PAGE_DEFINITION, ConfluenceCreatePageExecutor(client))
    register_tool(UPDATE_CONFLUENCE_PAGE_DEFINITION, ConfluenceUpdatePageExecutor(client))
    register_tool(ADD_CONFLUENCE_COMMENT_DEFINITION, ConfluenceAddCommentExecutor(client))
    register_tool(DELETE_CONFLUENCE_PAGE_DEFINITION, ConfluenceDeletePageExecutor(client))
    logger.info(f"Registered 6 Confluence tools (2 read + 4 write) for {domain or 'unconfigured'}")
