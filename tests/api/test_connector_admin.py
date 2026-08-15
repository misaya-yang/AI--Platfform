from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from src.api.deps import AuthContext
from src.api.schemas.connectors import (
    ConnectorProviderCreate,
    ConnectorProviderUpdate,
    ConnectorToggleRequest,
)
from src.api.v1.connector_admin import (
    create_connector_config,
    delete_connector_config,
    list_connector_configs,
    toggle_connector_config,
    update_connector_config,
)
from src.config.settings import Settings
from src.core.auth.user_resolver import UserContext

_INSERT_RE = re.compile(
    r"INSERT INTO connector_configs \((?P<cols>[^)]+)\) VALUES \((?P<ph>[^)]+)\)"
)
_UPDATE_CONFIG_RE = re.compile(
    r"UPDATE connector_configs SET (?P<sets>.+?),[ \t\n]*updated_at = NOW\(\)\s+"
    r"WHERE tenant_id = \$(?P<tid_idx>\d+) AND provider = \$(?P<pid_idx>\d+)"
)
_SELECT_ONE_RE = re.compile(
    r"SELECT \* FROM connector_configs\s+WHERE tenant_id = \$1 AND provider = \$2"
)
_SELECT_SCOPE_RE = re.compile(
    r"SELECT \* FROM connector_configs\s+WHERE provider = \$1 AND "
    r"\(tenant_id = \$2 OR tenant_id = ''\)\s+ORDER BY tenant_id DESC LIMIT 1"
)


class _FakeDb:
    """In-memory connector_configs/user_connectors store driven by query text."""

    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []
        self.user_connectors: list[dict[str, Any]] = []
        self.enabled = True

    # -- helpers ---------------------------------------------------------

    def _visible(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            row for row in self.configs if row.get("tenant_id") in {tenant_id, ""}
        ]

    def _row(self, tenant_id: str, provider: str) -> dict[str, Any] | None:
        rows = [
            row
            for row in self.configs
            if row.get("provider") == provider and row.get("tenant_id") in {tenant_id, ""}
        ]
        return sorted(rows, key=lambda r: r.get("tenant_id") or "", reverse=True)[0] if rows else None

    # -- async DB surface -------------------------------------------------

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "SELECT 1 FROM connector_configs" in query:
            tenant_id, provider = args
            return (
                {"present": True}
                if any(
                    row.get("tenant_id") == tenant_id and row.get("provider") == provider
                    for row in self.configs
                )
                else None
            )
        if _SELECT_ONE_RE.search(query):
            tenant_id, provider = args
            return next(
                (
                    dict(row)
                    for row in self.configs
                    if row.get("tenant_id") == tenant_id and row.get("provider") == provider
                ),
                None,
            )
        if _SELECT_SCOPE_RE.search(query):
            provider, tenant_id = args
            row = self._row(tenant_id, provider)
            return dict(row) if row else None
        raise AssertionError(f"unhandled fetchrow query: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "SELECT 1 FROM user_connectors" in query:
            if "tenant_id = $1" in query:
                tenant_id, provider = args
                return [
                    row
                    for row in self.user_connectors
                    if row.get("tenant_id") == tenant_id and row.get("provider") == provider
                ]
            provider = args[0]
            return [row for row in self.user_connectors if row.get("provider") == provider]
        if "SELECT * FROM connector_configs" in query and "WHERE" in query:
            # mirror SQL ORDER BY display_name, provider
            rows = sorted(
                self._visible(args[0]),
                key=lambda r: (str(r.get("display_name") or ""), str(r.get("provider") or "")),
            )
            return [dict(row) for row in rows]
        raise AssertionError(f"unhandled fetch query: {query}")

    async def execute(self, query: str, *args: Any) -> None:
        match = _INSERT_RE.search(query)
        if match:
            columns = [col.strip() for col in match.group("cols").split(",")]
            values = list(args)
            row = dict(zip(columns, values, strict=True))
            if "tenant_id" not in row:
                raise AssertionError("insert missing tenant_id")
            if any(
                r.get("tenant_id") == row["tenant_id"] and r.get("provider") == row["provider"]
                for r in self.configs
            ):
                raise AssertionError("duplicate (tenant_id, provider) insert")
            self.configs.append(row)
            return
        match = _UPDATE_CONFIG_RE.search(query)
        if match:
            tid_idx = int(match.group("tid_idx"))
            pid_idx = int(match.group("pid_idx"))
            tenant_id = args[tid_idx - 1]
            provider = args[pid_idx - 1]
            row = self._row(tenant_id, provider)
            if row is None:
                raise AssertionError("update on missing row")
            assignments = match.group("sets").split(",")
            for index, assignment in enumerate(assignments):
                column = assignment.strip().split("=")[0].strip()
                if column == "extra_config":
                    # COALESCE(extra_config, '{}'::jsonb) || $1::jsonb
                    json_value = args[index]
                    existing = row.get("extra_config") or {}
                    merged = dict(existing)
                    merged.update(json.loads(json_value))
                    row["extra_config"] = merged
                else:
                    row[column] = args[index]
            return
        if query.startswith("DELETE FROM connector_configs"):
            tenant_id, provider = args
            row = self._row(tenant_id, provider)
            if row is None:
                raise AssertionError("delete on missing row")
            self.configs.remove(row)
            return
        raise AssertionError(f"unhandled execute query: {query}")


def _request(db: _FakeDb) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(settings=Settings(), database=db)),
    )


def _auth() -> AuthContext:
    return AuthContext(
        user_id="admin",
        tenant_id="tenant-a",
        roles=["developer"],
        permissions=["console:settings:view"],
        is_authenticated=True,
    )


def _user() -> UserContext:
    return UserContext(
        user_id="admin",
        tenant_id="tenant-a",
        tier="enterprise",
        is_authenticated=True,
        roles=["developer"],
        ip="127.0.0.1",
    )


def _config_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "provider": "confluence",
        "display_name": "Confluence",
        "description": None,
        "icon_url": None,
        "client_id": "client-123",
        "client_secret": "super-secret",
        "auth_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": "read write",
        "redirect_uri": None,
        "enabled": True,
        "supports_sync": False,
        "supports_search": True,
        "mode": "live",
        "extra_config": {},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    row.update(changes)
    return row


def _create_payload(**changes: Any) -> ConnectorProviderCreate:
    data: dict[str, Any] = {
        "provider": "jira",
        "display_name": "Jira",
        "description": "Issue tracker",
        "mode": "live",
        "auth": {
            "client_id": "cid",
            "client_secret": "csecret",
            "auth_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "scopes": "read",
        },
        "mcp_tools": [{"name": "jira_search", "description": "Search issues"}],
    }
    data.update(changes)
    return ConnectorProviderCreate(**data)


# ─── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_configs_includes_global_and_tenant_rows_without_client_secret() -> None:
    db = _FakeDb()
    db.configs = [
        _config_row(tenant_id="", provider="github"),
        _config_row(tenant_id="tenant-a", provider="confluence"),
    ]
    request = _request(db)

    result = await list_connector_configs(request=request, user=_user(), auth=_auth())

    providers = [item["provider"] for item in result]
    assert providers == ["confluence", "github"]
    for item in result:
        assert "client_secret" not in item
        assert item["auth"]["client_id"] == "client-123"
        assert item["mode"] == "live"


@pytest.mark.asyncio
async def test_create_config_persists_secret_but_never_echoes_it() -> None:
    db = _FakeDb()
    request = _request(db)

    result = await create_connector_config(
        payload=_create_payload(), request=request, user=_user(), auth=_auth()
    )

    assert result["provider"] == "jira"
    assert "client_secret" not in result
    stored = db._row("tenant-a", "jira")
    assert stored is not None
    assert stored["client_secret"] == "csecret"
    assert stored["extra_config"]["mcp_tools"] == [
        {"name": "jira_search", "description": "Search issues"}
    ]


@pytest.mark.asyncio
async def test_create_config_conflicts_on_existing_provider() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="jira")]
    request = _request(db)

    with pytest.raises(HTTPException) as exc:
        await create_connector_config(
            payload=_create_payload(), request=request, user=_user(), auth=_auth()
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_update_config_keeps_existing_secret_when_empty() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    result = await update_connector_config(
        provider="confluence",
        payload=ConnectorProviderUpdate(
            display_name="Confluence Cloud",
            mode="both",
            auth={"client_id": "new-cid", "client_secret": None},
        ),
        request=request,
        user=_user(),
        auth=_auth(),
    )

    assert result["display_name"] == "Confluence Cloud"
    assert result["mode"] == "both"
    stored = db._row("tenant-a", "confluence")
    assert stored["client_secret"] == "super-secret"
    assert stored["client_id"] == "new-cid"


@pytest.mark.asyncio
async def test_update_config_replaces_secret_when_provided() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    result = await update_connector_config(
        provider="confluence",
        payload=ConnectorProviderUpdate(
            auth={"client_secret": "rotated-secret"},
        ),
        request=request,
        user=_user(),
        auth=_auth(),
    )

    assert "client_secret" not in result
    assert db._row("tenant-a", "confluence")["client_secret"] == "rotated-secret"


@pytest.mark.asyncio
async def test_update_config_extra_only_does_not_emit_empty_set_clause() -> None:
    """Regression: mcp_tools-only updates must not build `SET , updated_at` SQL.

    The column SET list is empty for an extra-only payload; the endpoint must
    skip the column UPDATE entirely and only merge the JSONB extra_config.
    """
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    result = await update_connector_config(
        provider="confluence",
        payload=ConnectorProviderUpdate(
            mcp_tools=[{"name": "confluence_read", "description": "Read pages"}],
        ),
        request=request,
        user=_user(),
        auth=_auth(),
    )

    assert result["mcp_tools"] == [
        {"name": "confluence_read", "description": "Read pages"}
    ]
    stored = db._row("tenant-a", "confluence")
    assert stored["display_name"] == "Confluence"  # untouched by the extra-only update


@pytest.mark.asyncio
async def test_update_config_rejects_empty_payload() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    with pytest.raises(HTTPException) as exc:
        await update_connector_config(
            provider="confluence",
            payload=ConnectorProviderUpdate(),
            request=request,
            user=_user(),
            auth=_auth(),
        )

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_toggle_enabled_flips_the_flag() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    result = await toggle_connector_config(
        provider="confluence",
        payload=ConnectorToggleRequest(enabled=False),
        request=request,
        user=_user(),
        auth=_auth(),
    )

    assert result["enabled"] is False
    assert db._row("tenant-a", "confluence")["enabled"] is False


@pytest.mark.asyncio
async def test_delete_config_refused_while_user_connectors_exist() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    db.user_connectors = [
        {
            "tenant_id": "tenant-a",
            "user_id": "user-1",
            "provider": "confluence",
            "status": "connected",
        }
    ]
    request = _request(db)

    with pytest.raises(HTTPException) as exc:
        await delete_connector_config(
            provider="confluence", request=request, user=_user(), auth=_auth()
        )

    assert exc.value.status_code == 409
    assert db._row("tenant-a", "confluence") is not None


@pytest.mark.asyncio
async def test_delete_config_succeeds_when_no_connections() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)

    result = await delete_connector_config(
        provider="confluence", request=request, user=_user(), auth=_auth()
    )

    assert result["status"] == "deleted"
    assert db._row("tenant-a", "confluence") is None


@pytest.mark.asyncio
async def test_global_config_delete_checks_all_tenants() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="", provider="github")]
    db.user_connectors = [
        {
            "tenant_id": "tenant-b",
            "user_id": "user-2",
            "provider": "github",
            "status": "connected",
        }
    ]
    request = _request(db)

    with pytest.raises(HTTPException) as exc:
        await delete_connector_config(
            provider="github", request=request, user=_user(), auth=_auth()
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_missing_provider_returns_404() -> None:
    db = _FakeDb()
    request = _request(db)

    with pytest.raises(HTTPException) as exc:
        await delete_connector_config(
            provider="nope", request=request, user=_user(), auth=_auth()
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoints_deny_callers_without_settings_permission() -> None:
    db = _FakeDb()
    db.configs = [_config_row(tenant_id="tenant-a", provider="confluence")]
    request = _request(db)
    auth = AuthContext(
        user_id="user-1",
        tenant_id="tenant-a",
        roles=["user"],
        permissions=[],
        is_authenticated=True,
    )

    with pytest.raises(HTTPException) as exc:
        await list_connector_configs(request=request, user=_user(), auth=auth)

    assert exc.value.status_code == 403
