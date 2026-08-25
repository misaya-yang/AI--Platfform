"""Internal, scope-bound broker for complete redacted tool-output artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from ai_gateway_core.auth.capability_proof import CapabilityProofError, verify_capability_proof
from ai_gateway_core.storage import get_artifact_storage
from fastapi import APIRouter, Body, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...core.assistant_capability_catalog import load_assistant_capability_catalog

router = APIRouter(
    prefix="/internal/v2/agent-capabilities",
    tags=["internal-agent-capabilities"],
)

_ARTIFACT_ID = re.compile(r"^art_[A-Za-z0-9]{8,64}$")
_MAX_BYTES = 2_000_000
_REQUIRED_METADATA = {
    "schema_version": "assistant-tool-output-artifact/v1",
    "redacted": True,
    "complete_redacted": True,
    "content_kind": "text",
}

_CATALOG_REQUEST_SCHEMA_VERSION = "capability-catalog/v2"
_CATALOG_RESPONSE_SCHEMA_VERSION = "capability-catalog/v2"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024
_MAX_CATALOG_ENTRIES = 256
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_WEB_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024


class _HTTPResponse(Protocol):
    status_code: int
    content: bytes

    def json(self) -> Any: ...


class CapabilityCatalogRequest(BaseModel):
    """Runtime's legacy catalog request, brokered to the Rust Worker."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    session_id: str = Field(min_length=1, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    capability_revision: int = Field(ge=1)
    capability_allowlist: list[dict[str, Any]] | None = None


class ArtifactReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0, le=_MAX_BYTES)
    limit: int = Field(default=64_000, ge=1, le=_MAX_BYTES)


class WebSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(min_length=1, max_length=5)
    max_results: int = Field(default=5, ge=1, le=10)


def _scope_header(value: str | None, field: str) -> str:
    if (
        not value
        or len(value) > 255
        or any(ord(character) < 32 for character in value)
    ):
        raise HTTPException(status_code=403, detail=f"{field} scope is invalid")
    return value


def _user_has_permissions(roles: set[str], permissions: set[str], required: list[str]) -> bool:
    """Apply the catalog's permission lattice without trusting Worker input."""

    if not required:
        return True
    subjects = roles | permissions
    if "admin" in subjects:
        return True
    tier_order = {"anonymous": 0, "normal": 1, "premium": 2, "enterprise": 3, "admin": 4}
    tier = next((subject.split(":", 1)[1].lower() for subject in subjects if subject.startswith("tier:")), "normal")
    for permission in required:
        if permission.startswith("role:"):
            if permission.split(":", 1)[1].strip() not in subjects:
                return False
        elif permission.startswith("tier:"):
            required_tier = permission.split(":", 1)[1].strip().lower()
            if required_tier not in tier_order or tier not in tier_order:
                return False
            if tier_order[tier] < tier_order[required_tier]:
                return False
        elif permission not in subjects:
            return False
    return True


def _descriptor_kind(descriptor: dict[str, Any], record: dict[str, Any] | None) -> str:
    if record is not None:
        kind = record.get("kind")
        if kind in {"tool", "knowledge", "mcp", "connector", "office_read", "platform_tool_discovery"}:
            return str(kind)
    tags = descriptor.get("tags")
    if not isinstance(tags, list):
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    for kind in ("knowledge", "mcp", "connector", "platform_tool_discovery", "tool"):
        if f"kind:{kind}" in tags:
            return kind
    if "fixture" in tags:
        return "tool"
    raise HTTPException(status_code=503, detail="capability catalog is invalid")


def _project_worker_descriptor(
    descriptor: dict[str, Any],
    *,
    tenant_id: str,
    capability_revision: int,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    if set(descriptor) - {
        "schema_version",
        "id",
        "name",
        "version",
        "description",
        "schema_hash",
        "input_schema",
        "output_schema",
        "effect",
        "approval_policy",
        "execution_mode",
        "timeout_ms",
        "tags",
        "protocol",
        "connector_binding",
    }:
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    name = descriptor.get("name")
    schema = descriptor.get("input_schema")
    schema_hash = descriptor.get("schema_hash")
    if (
        descriptor.get("schema_version") != "capability-descriptor/v2"
        or not isinstance(name, str)
        or descriptor.get("id") != name
        or not isinstance(descriptor.get("description"), str)
        or not isinstance(schema, dict)
        or not isinstance(schema_hash, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", schema_hash)
        or descriptor.get("effect") not in {"read", "write", "unknown"}
        or descriptor.get("approval_policy") not in {"never", "on_request", "always"}
        or not isinstance(descriptor.get("tags"), list)
    ):
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    kind = _descriptor_kind(descriptor, record)
    version = descriptor.get("version")
    if version == "null":
        version = None
    if record is not None and (
        schema_hash != record.get("schema_hash")
        or descriptor.get("effect") != record.get("effect")
        or descriptor.get("protocol") != record.get("protocol")
        or version != record.get("version")
        or f"kind:{record.get('kind')}" not in descriptor["tags"]
    ):
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    read_only = descriptor["effect"] == "read"
    if read_only and descriptor["approval_policy"] != "never":
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    if not read_only and descriptor["approval_policy"] == "never":
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    return {
        "name": name,
        "id": descriptor["id"],
        "version": version,
        "schema_hash": schema_hash,
        "description": descriptor["description"],
        "schema": schema,
        "output_schema": descriptor["output_schema"],
        "source": "capability_worker",
        "kind": kind,
        "read_only": read_only,
        "effect": descriptor["effect"],
        "approval_policy": descriptor["approval_policy"],
        "execution_mode": descriptor["execution_mode"],
        "timeout_ms": descriptor["timeout_ms"],
        "tags": list(descriptor["tags"]),
        "protocol": descriptor["protocol"],
        "tenant_id": tenant_id,
        "capability_revision": capability_revision,
        **(
            {"connector_binding": descriptor["connector_binding"]}
            if isinstance(descriptor.get("connector_binding"), dict)
            else {}
        ),
    }


async def _worker_catalog_response(
    request: Request,
    *,
    scope: dict[str, str],
    capability_revision: int,
) -> _HTTPResponse:
    worker_url = os.getenv("AI_PLATFORM_CAPABILITY_WORKER_URL", "").strip().rstrip("/")
    token = os.getenv("AI_PLATFORM_INTERNAL_TOKEN", "").strip()
    if not worker_url or not token:
        raise HTTPException(status_code=503, detail="capability worker unavailable")
    body = {
        "schema_version": _CATALOG_REQUEST_SCHEMA_VERSION,
        **scope,
        "capability_revision": capability_revision,
    }
    headers = {
        "x-ai-platform-internal-token": token,
        "x-ai-tenant-id": scope["tenant_id"],
        "x-ai-user-id": scope["user_id"],
        "x-ai-session-id": scope["session_id"],
    }
    injected = getattr(request.app.state, "agent_capability_worker_client", None)
    if injected is not None:
        return await injected.post(
            f"{worker_url}/internal/v2/capabilities/catalog",
            headers=headers,
            json=body,
        )
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
        ) as client:
            return await client.post(
                f"{worker_url}/internal/v2/capabilities/catalog",
                headers=headers,
                json=body,
            )
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="capability worker unavailable") from None


@router.post("/catalog")
async def broker_agent_capability_catalog(
    payload: CapabilityCatalogRequest,
    request: Request,
    x_ai_platform_internal_token: str | None = Header(default=None),
    x_ai_tenant_id: str | None = Header(default=None),
    x_ai_user_id: str | None = Header(default=None),
    x_ai_session_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Broker the Worker catalog into the Runtime's tenant-scoped V1 shape."""

    expected = os.getenv("AI_PLATFORM_INTERNAL_TOKEN", "")
    if not expected or not x_ai_platform_internal_token or not hmac.compare_digest(
        x_ai_platform_internal_token, expected
    ):
        raise HTTPException(status_code=401, detail="internal authorization failed")
    scope = {
        "tenant_id": _scope_header(x_ai_tenant_id, "tenant"),
        "user_id": _scope_header(x_ai_user_id, "user"),
        "session_id": _scope_header(x_ai_session_id, "session"),
    }
    if any(getattr(payload, key) != value for key, value in scope.items()):
        raise HTTPException(status_code=403, detail="catalog scope mismatch")
    database = getattr(request.app.state, "database", None)
    if database is None or getattr(database, "enabled", True) is False:
        raise HTTPException(status_code=503, detail="catalog identity unavailable")
    get_user = getattr(database, "get_user_for_tenant", None)
    if not callable(get_user):
        raise HTTPException(status_code=503, detail="catalog identity unavailable")
    try:
        user = await get_user(scope["user_id"], scope["tenant_id"])
    except Exception:
        raise HTTPException(status_code=503, detail="catalog identity unavailable") from None
    if not user:
        raise HTTPException(status_code=403, detail="catalog scope is not authorized")
    roles = {str(role) for role in (user.get("roles") or [])}
    permissions = {str(permission) for permission in (user.get("permissions") or [])}
    for method_name, target in (("get_user_roles", roles), ("get_user_permissions", permissions)):
        method = getattr(database, method_name, None)
        if callable(method):
            try:
                target.update(str(value) for value in await method(scope["user_id"]))
            except Exception:
                raise HTTPException(status_code=503, detail="catalog identity unavailable") from None
    policy = {}
    fetchrow = getattr(database, "fetchrow", None)
    if callable(fetchrow):
        try:
            row = await fetchrow(
                "SELECT allowed_tools, blocked_tools, allowed_categories "
                "FROM tenant_tool_policies WHERE tenant_id = $1",
                scope["tenant_id"],
            )
            policy = dict(row) if row else {}
        except Exception:
            raise HTTPException(status_code=503, detail="catalog policy unavailable") from None
    try:
        _, records = load_assistant_capability_catalog()
    except Exception:
        raise HTTPException(status_code=503, detail="capability catalog unavailable") from None
    record_by_id = {str(record["id"]): record for record in records}
    response = await _worker_catalog_response(
        request,
        scope=scope,
        capability_revision=payload.capability_revision,
    )
    if response.status_code >= 400 or len(response.content) > _MAX_CATALOG_BYTES:
        raise HTTPException(status_code=503, detail="capability worker unavailable")
    try:
        envelope = response.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=503, detail="capability catalog is invalid") from None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != _CATALOG_RESPONSE_SCHEMA_VERSION
        or envelope.get("capability_revision") != payload.capability_revision
        or not isinstance(envelope.get("capabilities"), list)
        or len(envelope["capabilities"]) > _MAX_CATALOG_ENTRIES
    ):
        raise HTTPException(status_code=503, detail="capability catalog is invalid")
    allowed_tools = set(policy.get("allowed_tools") or [])
    blocked_tools = set(policy.get("blocked_tools") or [])
    allowed_categories = set(policy.get("allowed_categories") or [])
    tools: list[dict[str, Any]] = []
    mcp: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for raw in envelope["capabilities"]:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=503, detail="capability catalog is invalid")
        record = record_by_id.get(str(raw.get("id") or ""))
        name = str(raw.get("name") or "")
        # The provider-native Responses search tool is the zero-secret default.
        # Publish the Worker/Tavily implementation only when its dedicated key
        # is configured; otherwise two equivalent tools are advertised and the
        # model can select a capability that is guaranteed to fail.
        if name == "search_web" and not os.getenv("TAVILY_API_KEY", "").strip():
            continue
        category = str(record.get("category") or "") if record else ""
        required = list(record.get("required_permissions") or []) if record else []
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        required.extend(str(tag)[11:] for tag in tags if str(tag).startswith("permission:"))
        if not _user_has_permissions(roles, permissions, list(dict.fromkeys(required))):
            continue
        if allowed_tools and name not in allowed_tools:
            continue
        if name in blocked_tools or (allowed_categories and category not in allowed_categories):
            continue
        descriptor = _project_worker_descriptor(
            raw,
            tenant_id=scope["tenant_id"],
            capability_revision=payload.capability_revision,
            record=record,
        )
        if descriptor["read_only"]:
            (mcp if descriptor["kind"] == "mcp" else tools).append(descriptor)
        else:
            deferred.append(descriptor)
    return {
        "schema_version": "agent-capability-catalog/v1",
        "capability_revision": payload.capability_revision,
        "tools": tools,
        "mcp": mcp,
        "deferred": deferred,
    }


def _authorize(
    internal_token: str | None,
    tenant_id: str | None,
    user_id: str | None,
    session_id: str | None,
    proof: str | None,
    execution_id: str | None,
    run_id: str | None,
    *,
    path: str,
    body: Any,
) -> tuple[str, str, str]:
    expected = os.getenv("AI_PLATFORM_INTERNAL_TOKEN", "")
    if not expected or not internal_token or not hmac.compare_digest(internal_token, expected):
        raise HTTPException(status_code=401, detail="internal authorization failed")
    values = (tenant_id, user_id, session_id)
    if any(
        not value or len(value) > 255 or any(ord(char) < 32 for char in value) for value in values
    ):
        raise HTTPException(status_code=403, detail="scope is invalid")
    proof_secret = os.getenv("AI_PLATFORM_CAPABILITY_PROOF_SECRET", "")
    if not proof_secret or not proof or not execution_id or not run_id:
        raise HTTPException(status_code=401, detail="capability proof required")
    try:
        verify_capability_proof(
            proof_secret,
            proof,
            method="POST",
            path=path,
            body=body,
            tenant_id=values[0],
            user_id=values[1],
            session_id=values[2],
            execution_id=execution_id,
            run_id=run_id,
        )
    except CapabilityProofError:
        raise HTTPException(status_code=401, detail="capability proof invalid") from None
    return values[0], values[1], values[2]  # type: ignore[return-value]


def _bounded_search_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    raw_url = value.get("url")
    content = value.get("content")
    if not isinstance(title, str) or not isinstance(raw_url, str) or not isinstance(content, str):
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or len(raw_url) > 4096:
        return None
    score = value.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float):
        score = 0.0
    return {
        "title": title[:500],
        "url": raw_url,
        "content": content[:4000],
        "score": max(0.0, min(float(score), 1.0)),
    }


async def _search_one_query(
    client: Any,
    *,
    api_key: str,
    query: str,
    max_results: int,
) -> dict[str, Any]:
    response = await client.post(
        _TAVILY_SEARCH_URL,
        headers={"authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        },
    )
    if response.status_code >= 400 or len(response.content) > _MAX_WEB_SEARCH_RESPONSE_BYTES:
        raise HTTPException(status_code=503, detail="web search unavailable")
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=503, detail="web search unavailable") from None
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list):
        raise HTTPException(status_code=503, detail="web search unavailable")
    results = [
        result
        for item in raw_results[:max_results]
        if (result := _bounded_search_result(item)) is not None
    ]
    return {"query": query, "results": results}


@router.post("/web-search")
async def search_agent_web(
    request: Request,
    payload: WebSearchRequest = Body(...),
    x_ai_platform_internal_token: str | None = Header(default=None),
    x_ai_tenant_id: str | None = Header(default=None),
    x_ai_user_id: str | None = Header(default=None),
    x_ai_session_id: str | None = Header(default=None),
    x_ai_capability_proof: str | None = Header(default=None),
    x_ai_execution_id: str | None = Header(default=None),
    x_ai_run_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Execute bounded public search without sharing the provider key with Worker."""

    body = payload.model_dump(mode="json")
    _authorize(
        x_ai_platform_internal_token,
        x_ai_tenant_id,
        x_ai_user_id,
        x_ai_session_id,
        x_ai_capability_proof,
        x_ai_execution_id,
        x_ai_run_id,
        path="/internal/v2/agent-capabilities/web-search",
        body=body,
    )
    queries = [query.strip() for query in payload.queries]
    if any(not query or len(query) > 500 or any(ord(char) < 32 for char in query) for query in queries):
        raise HTTPException(status_code=422, detail="web search query is invalid")
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="web search unavailable")
    injected = getattr(request.app.state, "agent_web_search_client", None)
    if injected is not None:
        results = await asyncio.gather(
            *(
                _search_one_query(
                    injected,
                    api_key=api_key,
                    query=query,
                    max_results=payload.max_results,
                )
                for query in queries
            )
        )
    else:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=3.0, read=15.0, write=5.0, pool=3.0),
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
                trust_env=False,
            ) as client:
                results = await asyncio.gather(
                    *(
                        _search_one_query(
                            client,
                            api_key=api_key,
                            query=query,
                            max_results=payload.max_results,
                        )
                        for query in queries
                    )
                )
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError):
            raise HTTPException(status_code=503, detail="web search unavailable") from None
    return {
        "schema_version": "agent-web-search-result/v1",
        "provider": "tavily",
        "queries": results,
    }


@router.post("/artifacts/{artifact_id}/read")
async def read_agent_capability_artifact(
    artifact_id: str,
    payload: ArtifactReadRequest = Body(...),
    x_ai_platform_internal_token: str | None = Header(default=None),
    x_ai_tenant_id: str | None = Header(default=None),
    x_ai_user_id: str | None = Header(default=None),
    x_ai_session_id: str | None = Header(default=None),
    x_ai_capability_proof: str | None = Header(default=None),
    x_ai_execution_id: str | None = Header(default=None),
    x_ai_run_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return a bounded UTF-8 character slice, never a path, URL, or redirect."""

    tenant_id, user_id, session_id = _authorize(
        x_ai_platform_internal_token,
        x_ai_tenant_id,
        x_ai_user_id,
        x_ai_session_id,
        x_ai_capability_proof,
        x_ai_execution_id,
        x_ai_run_id,
        path=f"/internal/v2/agent-capabilities/artifacts/{artifact_id}/read",
        body=payload.model_dump(mode="json"),
    )
    if not _ARTIFACT_ID.fullmatch(artifact_id):
        raise HTTPException(status_code=404, detail="artifact not found")
    storage = get_artifact_storage()
    reader = getattr(storage, "read_artifact_scoped", None) if storage else None
    if not callable(reader):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        scoped = await reader(
            artifact_id,
            max_bytes=_MAX_BYTES,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        # Missing rows, schema, and backend failures share the non-enumerating contract.
        raise HTTPException(status_code=404, detail="artifact not found") from None
    if not scoped:
        raise HTTPException(status_code=404, detail="artifact not found")
    artifact, raw = scoped
    metadata = dict(getattr(artifact, "metadata", None) or {})
    if (
        getattr(artifact, "source", None) != "tool_output_spill"
        or any(metadata.get(key) != value for key, value in _REQUIRED_METADATA.items())
        or not isinstance(raw, bytes)
        or len(raw) > _MAX_BYTES
    ):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise HTTPException(status_code=404, detail="artifact not found") from None
    digest = hashlib.sha256(raw).hexdigest()
    if metadata.get("content_sha256") != digest or metadata.get("content_chars") != len(text):
        raise HTTPException(status_code=404, detail="artifact not found")
    receipt_id = str(metadata.get("host_receipt_id") or "")
    if not receipt_id or str(getattr(artifact, "turn_id", "") or "") != receipt_id:
        raise HTTPException(status_code=404, detail="artifact not found")
    if payload.offset > len(text):
        raise HTTPException(status_code=416, detail="offset out of range")
    content = text[payload.offset : payload.offset + payload.limit]
    next_offset = (
        payload.offset + len(content) if payload.offset + len(content) < len(text) else None
    )
    return {
        "artifact_id": artifact_id,
        "content": content,
        "offset": payload.offset,
        "next_offset": next_offset,
        "total_chars": len(text),
        "content_sha256": digest,
        "complete": next_offset is None,
        "artifact_complete": True,
        "redacted": True,
        "redaction_receipt": {
            "schema_version": _REQUIRED_METADATA["schema_version"],
            "complete_redacted": True,
            "host_verified": True,
        },
    }
