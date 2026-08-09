"""Confluence REST client, isolated from tool definitions and executors."""

from __future__ import annotations

import re
from base64 import b64encode
from typing import Any

import httpx
from ai_gateway_core.logging import get_logger

from .confluence_format import (
    _escape_for_storage,
    _excerpt_from_html,
    _filter_and_rank_spaces,
    _html_to_structured_text,
    _markdown_to_storage,
)

logger = get_logger(__name__)


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
