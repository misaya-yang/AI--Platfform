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

import time
from typing import Any

import httpx
from ai_gateway_core.logging import get_logger

from .confluence_client import ConfluenceAPIClient
from .confluence_format import (
    _escape_for_storage,
    _excerpt_from_html,
    _filter_and_rank_spaces,
    _html_to_structured_text,
    _markdown_to_storage,
)
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

__all__ = [
    "CONFLUENCE_READ_DEFINITION",
    "CONFLUENCE_WRITE_DEFINITION",
    "ConfluenceAPIClient",
    "ConfluenceReadExecutor",
    "ConfluenceWriteExecutor",
    "_classify_http_error",
    "_escape_for_storage",
    "_excerpt_from_html",
    "_filter_and_rank_spaces",
    "_html_to_structured_text",
    "_markdown_to_storage",
    "register_confluence_tools",
]


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
