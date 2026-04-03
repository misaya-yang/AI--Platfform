"""
Connector Management API — OAuth-based third-party integrations.

Supports: Confluence, Outlook, GitHub, Gmail, Google Drive, Slack.

Endpoints:
  GET    /connectors/available          — List configured connectors
  GET    /connectors/mine               — List user's connected services
  GET    /connectors/auth/{provider}    — Initiate OAuth flow (returns redirect URL)
  GET    /connectors/callback/{provider} — OAuth callback (exchanges code for tokens)
  DELETE /connectors/{provider}         — Disconnect a connector
  POST   /connectors/{provider}/search  — Search connector data (e.g., Confluence pages)
"""

from __future__ import annotations

import secrets
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from ...core.auth.user_resolver import UserContext, get_current_user
from ...core.database import get_database
from ...core.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/connectors", tags=["Connectors"])


# ─── Models ───────────────────────────────────────────────────────────

class ConnectorInfo(BaseModel):
    provider: str
    display_name: str
    description: str | None = None
    icon_url: str | None = None
    enabled: bool = True
    connected: bool = False
    status: str | None = None


class ConnectorSearchRequest(BaseModel):
    query: str
    limit: int = 10


# ─── Helpers ──────────────────────────────────────────────────────────

async def _get_connector_config(db, provider: str, tenant_id: str = "") -> dict | None:
    row = await db.fetchrow(
        """SELECT * FROM connector_configs
           WHERE provider = $1 AND (tenant_id = $2 OR tenant_id = '')
           AND enabled = true
           ORDER BY tenant_id DESC LIMIT 1""",
        provider, tenant_id,
    )
    return dict(row) if row else None


async def _get_user_connector(db, tenant_id: str, user_id: str, provider: str) -> dict | None:
    row = await db.fetchrow(
        """SELECT * FROM user_connectors
           WHERE tenant_id = $1 AND user_id = $2 AND provider = $3""",
        tenant_id, user_id, provider,
    )
    return dict(row) if row else None


async def _save_user_connector(
    db, tenant_id: str, user_id: str, provider: str,
    access_token: str, refresh_token: str | None,
    expires_at: datetime | None, scopes: str,
    metadata: dict | None = None,
):
    await db.execute(
        """INSERT INTO user_connectors (tenant_id, user_id, provider, access_token, refresh_token,
           token_expires_at, token_scopes, provider_metadata, status, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'connected', NOW())
           ON CONFLICT (tenant_id, user_id, provider)
           DO UPDATE SET access_token = $4, refresh_token = $5, token_expires_at = $6,
                         token_scopes = $7, provider_metadata = COALESCE($8, user_connectors.provider_metadata),
                         status = 'connected', last_error = NULL, updated_at = NOW()""",
        tenant_id, user_id, provider, access_token, refresh_token,
        expires_at, scopes, metadata,
    )


async def _refresh_token_if_needed(db, connector: dict, config: dict) -> str:
    """Refresh OAuth token if expired. Returns valid access_token."""
    if connector.get("token_expires_at") and connector["token_expires_at"] < datetime.now(timezone.utc):
        if not connector.get("refresh_token"):
            raise HTTPException(400, "Token expired and no refresh token available. Reconnect.")

        async with httpx.AsyncClient() as client:
            resp = await client.post(config["token_url"], data={
                "grant_type": "refresh_token",
                "refresh_token": connector["refresh_token"],
                "client_id": config["client_id"],
                "client_secret": config.get("client_secret", ""),
            })
            if resp.status_code != 200:
                await db.execute(
                    "UPDATE user_connectors SET status = 'expired', last_error = $1, updated_at = NOW() WHERE id = $2",
                    f"Refresh failed: {resp.text[:200]}", connector["id"],
                )
                raise HTTPException(401, "Token refresh failed. Please reconnect.")

            data = resp.json()
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))
            await db.execute(
                """UPDATE user_connectors SET access_token = $1, refresh_token = COALESCE($2, refresh_token),
                   token_expires_at = $3, status = 'connected', updated_at = NOW() WHERE id = $4""",
                data["access_token"], data.get("refresh_token"), expires_at, connector["id"],
            )
            return data["access_token"]

    return connector["access_token"]


# ─── Endpoints ────────────────────────────────────────────────────────

@router.get("/available")
async def list_available_connectors(
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """List all configured connectors with user's connection status."""
    db = get_database()
    configs = await db.fetch(
        "SELECT * FROM connector_configs WHERE (tenant_id = $1 OR tenant_id = '') AND enabled = true",
        user.tenant_id,
    )
    user_conns = await db.fetch(
        "SELECT provider, status FROM user_connectors WHERE tenant_id = $1 AND user_id = $2",
        user.tenant_id, user.user_id,
    )
    conn_map = {r["provider"]: r["status"] for r in user_conns}

    return [
        ConnectorInfo(
            provider=c["provider"],
            display_name=c["display_name"],
            description=c.get("description"),
            icon_url=c.get("icon_url"),
            enabled=c["enabled"],
            connected=c["provider"] in conn_map and conn_map[c["provider"]] == "connected",
            status=conn_map.get(c["provider"]),
        )
        for c in configs
    ]


@router.get("/mine")
async def list_my_connectors(user: UserContext = Depends(get_current_user)):
    """List user's connected services."""
    db = get_database()
    rows = await db.fetch(
        """SELECT provider, display_name, status, last_used_at, provider_metadata, created_at
           FROM user_connectors WHERE tenant_id = $1 AND user_id = $2 AND status != 'revoked'""",
        user.tenant_id, user.user_id,
    )
    return [dict(r) for r in rows]


@router.get("/auth/{provider}")
async def initiate_oauth(
    provider: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
):
    """Initiate OAuth flow — returns the authorization URL to redirect the user."""
    db = get_database()
    config = await _get_connector_config(db, provider, user.tenant_id)
    if not config:
        raise HTTPException(404, f"Connector not configured: {provider}")
    if not config.get("client_id"):
        raise HTTPException(400, f"OAuth not configured for {provider}. Admin must set client_id.")

    state = f"{user.tenant_id}:{user.user_id}:{provider}:{secrets.token_urlsafe(16)}"

    # Build redirect URI
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = config.get("redirect_uri") or f"{base_url}/api/v1/connectors/callback/{provider}"

    extra_config = config.get("extra_config") or {}
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "scope": config["scopes"],
        "response_type": "code",
        "state": state,
    }
    # Provider-specific params
    if provider == "confluence":
        params["audience"] = extra_config.get("audience", "api.atlassian.com")
        params["prompt"] = "consent"
    if provider in ("outlook", "gmail", "google_drive"):
        params["access_type"] = "offline"
        params["prompt"] = "consent"

    auth_url = f"{config['auth_url']}?{urllib.parse.urlencode(params)}"
    return {"auth_url": auth_url, "state": state}


@router.get("/callback/{provider}")
async def oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(""),
    request: Request = None,
):
    """OAuth callback — exchanges authorization code for tokens."""
    # Parse state
    parts = state.split(":")
    if len(parts) < 3:
        raise HTTPException(400, "Invalid state parameter")
    tenant_id, user_id = parts[0], parts[1]

    db = get_database()
    config = await _get_connector_config(db, provider, tenant_id)
    if not config:
        raise HTTPException(404, f"Connector not configured: {provider}")

    base_url = str(request.base_url).rstrip("/") if request else ""
    redirect_uri = config.get("redirect_uri") or f"{base_url}/api/v1/connectors/callback/{provider}"

    # Exchange code for tokens
    token_data = {
        "grant_type": "authorization_code",
        "client_id": config["client_id"],
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if config.get("client_secret"):
        token_data["client_secret"] = config["client_secret"]

    headers = {"Accept": "application/json"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(config["token_url"], data=token_data, headers=headers)

    if resp.status_code != 200:
        logger.error(f"OAuth token exchange failed for {provider}: {resp.text[:300]}")
        raise HTTPException(400, f"Token exchange failed: {resp.text[:200]}")

    data = resp.json()
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)

    if not access_token:
        raise HTTPException(400, "No access_token in response")

    from datetime import timedelta
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Provider-specific metadata
    metadata: dict[str, Any] = {}
    if provider == "confluence":
        # Get accessible resources (cloud ID)
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://api.atlassian.com/oauth/token/accessible-resources",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if res.status_code == 200:
                resources = res.json()
                if resources:
                    metadata["cloud_id"] = resources[0]["id"]
                    metadata["site_url"] = resources[0].get("url", "")
                    metadata["site_name"] = resources[0].get("name", "")

    await _save_user_connector(
        db, tenant_id, user_id, provider,
        access_token, refresh_token, expires_at,
        config["scopes"], metadata,
    )

    logger.info(f"Connector connected: {provider} for user {user_id}")

    # Redirect back to frontend
    return {"status": "connected", "provider": provider, "metadata": metadata}


@router.delete("/{provider}")
async def disconnect_connector(
    provider: str,
    user: UserContext = Depends(get_current_user),
):
    """Disconnect a connector — revokes tokens."""
    db = get_database()
    await db.execute(
        """UPDATE user_connectors SET status = 'revoked', access_token = NULL,
           refresh_token = NULL, updated_at = NOW()
           WHERE tenant_id = $1 AND user_id = $2 AND provider = $3""",
        user.tenant_id, user.user_id, provider,
    )
    return {"status": "disconnected", "provider": provider}


@router.post("/{provider}/search")
async def search_connector(
    provider: str,
    body: ConnectorSearchRequest,
    user: UserContext = Depends(get_current_user),
):
    """Search connector data (e.g., Confluence pages, emails)."""
    db = get_database()
    connector = await _get_user_connector(db, user.tenant_id, user.user_id, provider)
    if not connector or connector["status"] != "connected":
        raise HTTPException(400, f"Not connected to {provider}. Connect first.")

    config = await _get_connector_config(db, provider, user.tenant_id)
    if not config:
        raise HTTPException(404, f"Connector not configured: {provider}")

    access_token = await _refresh_token_if_needed(db, connector, config)
    metadata = connector.get("provider_metadata") or {}
    extra = config.get("extra_config") or {}

    # Provider-specific search
    if provider == "confluence":
        return await _search_confluence(access_token, metadata, body.query, body.limit)
    elif provider == "outlook":
        return await _search_outlook(access_token, body.query, body.limit)
    elif provider == "github":
        return await _search_github(access_token, body.query, body.limit)
    elif provider == "gmail":
        return await _search_gmail(access_token, body.query, body.limit)
    else:
        raise HTTPException(400, f"Search not supported for {provider}")


# ─── Provider-specific search implementations ─────────────────────────

async def _search_confluence(token: str, metadata: dict, query: str, limit: int) -> list[dict]:
    cloud_id = metadata.get("cloud_id", "")
    if not cloud_id:
        raise HTTPException(400, "Confluence cloud_id not found. Reconnect.")

    cql = f'type=page AND text~"{query}"'
    url = f"https://api.atlassian.com/ex/confluence/{cloud_id}/wiki/rest/api/content/search"

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"cql": cql, "limit": limit, "expand": "body.view,space"},
                                headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Confluence search failed: {resp.text[:200]}")

    data = resp.json()
    results = []
    for page in data.get("results", []):
        body_html = page.get("body", {}).get("view", {}).get("value", "")
        # Strip HTML tags for text preview
        import re
        text = re.sub(r"<[^>]+>", "", body_html)[:500]
        results.append({
            "id": page["id"],
            "title": page["title"],
            "space": page.get("space", {}).get("name", ""),
            "url": f"{page.get('_links', {}).get('base', '')}{page.get('_links', {}).get('webui', '')}",
            "excerpt": text,
            "type": "confluence_page",
        })
    return results


async def _search_outlook(token: str, query: str, limit: int) -> list[dict]:
    url = "https://graph.microsoft.com/v1.0/me/messages"
    params = {"$search": f'"{query}"', "$top": limit, "$select": "subject,from,receivedDateTime,bodyPreview"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Outlook search failed: {resp.text[:200]}")

    data = resp.json()
    return [
        {
            "id": msg["id"],
            "title": msg.get("subject", ""),
            "from": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
            "date": msg.get("receivedDateTime", ""),
            "excerpt": msg.get("bodyPreview", "")[:300],
            "type": "email",
        }
        for msg in data.get("value", [])
    ]


async def _search_github(token: str, query: str, limit: int) -> list[dict]:
    url = "https://api.github.com/search/code"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"q": query, "per_page": limit},
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"GitHub search failed: {resp.text[:200]}")

    data = resp.json()
    return [
        {
            "id": item.get("sha", ""),
            "title": item.get("name", ""),
            "path": item.get("path", ""),
            "repo": item.get("repository", {}).get("full_name", ""),
            "url": item.get("html_url", ""),
            "type": "code",
        }
        for item in data.get("items", [])[:limit]
    ]


async def _search_gmail(token: str, query: str, limit: int) -> list[dict]:
    url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"q": query, "maxResults": limit},
                                headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Gmail search failed: {resp.text[:200]}")

    messages = resp.json().get("messages", [])
    results = []
    for msg in messages[:limit]:
        async with httpx.AsyncClient() as client:
            detail = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                headers={"Authorization": f"Bearer {token}"},
            )
        if detail.status_code == 200:
            headers = {h["name"]: h["value"] for h in detail.json().get("payload", {}).get("headers", [])}
            results.append({
                "id": msg["id"],
                "title": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "date": headers.get("Date", ""),
                "excerpt": detail.json().get("snippet", ""),
                "type": "email",
            })
    return results
