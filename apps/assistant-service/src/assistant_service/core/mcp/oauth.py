"""OAuth Authorization Code + PKCE helpers for tenant MCP connections.

The coordinator deliberately returns only an opaque Secret Store reference.
Access and refresh tokens never enter Agent specs, PostgreSQL rows, API
responses, logs, or exception messages.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.parse
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx

from .client import DNSResolver, MCPClient

_MAX_OAUTH_RESPONSE_BYTES = 64 * 1024
_SESSION_TTL_SECONDS = 600


class MCPOAuthError(RuntimeError):
    """Stable OAuth failure without provider, token, URL, or body detail."""

    def __init__(self, stable_code: str):
        self.stable_code = stable_code
        super().__init__(stable_code)


@dataclass(frozen=True)
class MCPOAuthSession:
    state: str
    tenant_id: str
    user_id: str
    server_id: str
    principal_type: str
    owner_user_id: str | None
    client_id: str
    redirect_uri: str
    token_endpoint: str
    resource: str
    audience: str
    scopes: tuple[str, ...]
    code_verifier: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class MCPOAuthGrant:
    secret_ref: str
    scopes: tuple[str, ...]
    audience: str
    expires_at: datetime
    refresh_configured: bool


class MCPOAuthSessionStore(Protocol):
    async def save(self, session: MCPOAuthSession) -> None: ...

    async def consume(self, state: str) -> MCPOAuthSession: ...


class MCPOAuthSecretWriter(Protocol):
    async def put_oauth_credential(
        self,
        *,
        tenant_id: str,
        server_id: str,
        owner_user_id: str | None,
        access_token: str,
        refresh_token: str | None,
        audience: str,
        scopes: tuple[str, ...],
        expires_at: datetime,
    ) -> str: ...


class InMemoryMCPOAuthSessionStore:
    """One-time session store for tests and single-process local development."""

    def __init__(self) -> None:
        self._sessions: dict[str, MCPOAuthSession] = {}
        self._lock = asyncio.Lock()

    async def save(self, session: MCPOAuthSession) -> None:
        async with self._lock:
            self._sessions[session.state] = session

    async def consume(self, state: str) -> MCPOAuthSession:
        async with self._lock:
            session = self._sessions.pop(state, None)
        if session is None:
            raise MCPOAuthError("MCP_OAUTH_STATE_INVALID")
        if session.expires_at <= datetime.now(timezone.utc):
            raise MCPOAuthError("MCP_OAUTH_STATE_EXPIRED")
        return session


class InMemoryMCPOAuthSecretStore:
    """Non-production Secret Store used by isolated protocol tests."""

    def __init__(self) -> None:
        self._credentials: dict[str, dict[str, Any]] = {}

    async def put_oauth_credential(
        self,
        *,
        tenant_id: str,
        server_id: str,
        owner_user_id: str | None,
        access_token: str,
        refresh_token: str | None,
        audience: str,
        scopes: tuple[str, ...],
        expires_at: datetime,
    ) -> str:
        secret_ref = f"memory-secret://mcp/{uuid.uuid4()}"
        self._credentials[secret_ref] = {
            "tenant_id": tenant_id,
            "server_id": server_id,
            "owner_user_id": owner_user_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "audience": audience,
            "scopes": scopes,
            "expires_at": expires_at,
        }
        return secret_ref

    async def resolve(self, secret_ref: str) -> str:
        item = self._credentials.get(secret_ref)
        if not item or item["expires_at"] <= datetime.now(timezone.utc):
            raise MCPOAuthError("MCP_SECRET_UNAVAILABLE")
        return str(item["access_token"])

    def __repr__(self) -> str:
        return f"InMemoryMCPOAuthSecretStore(refs={len(self._credentials)})"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _exact_audience(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return secrets.compare_digest(value, expected)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return len(value) == 1 and secrets.compare_digest(value[0], expected)
    return False


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class MCPOAuthCoordinator:
    """Discover OAuth metadata and exchange one-time PKCE sessions."""

    def __init__(
        self,
        *,
        session_store: MCPOAuthSessionStore,
        secret_writer: MCPOAuthSecretWriter,
        http_client: httpx.AsyncClient | None = None,
        dns_resolver: DNSResolver | None = None,
        timeout_seconds: float = 10.0,
        response_limit_bytes: int = _MAX_OAUTH_RESPONSE_BYTES,
    ) -> None:
        self._session_store = session_store
        self._secret_writer = secret_writer
        self._http = http_client
        self._owns_http = http_client is None
        self._dns_resolver = dns_resolver
        self._timeout_seconds = max(0.1, min(30.0, timeout_seconds))
        self._response_limit_bytes = max(1024, min(_MAX_OAUTH_RESPONSE_BYTES, response_limit_bytes))

    async def close(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
        self._http = None

    def _validate_endpoint(self, url: str) -> frozenset[str]:
        try:
            return MCPClient._validate_url(url, resolver=self._dns_resolver)
        except Exception as exc:
            raise MCPOAuthError("MCP_OAUTH_ENDPOINT_DENIED") from exc

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        addresses = self._validate_endpoint(url)
        http = self._http
        owns_request_client = http is None
        if http is None:
            # Metadata and token endpoints may have different authorities that
            # share an IP. A per-request pool prevents an IP-keyed connection
            # from reusing TLS negotiated for a different original hostname.
            http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        target, host_header, sni_hostname = MCPClient._pinned_request_target(
            url,
            addresses,
        )
        request = http.build_request(method, target, data=data)
        request.headers["Host"] = host_header
        request.headers["Connection"] = "close"
        request.extensions["sni_hostname"] = sni_hostname
        response: httpx.Response | None = None
        try:
            response = await http.send(
                request,
                stream=True,
                follow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise MCPOAuthError("MCP_OAUTH_REDIRECT_BLOCKED")
            if response.status_code < 200 or response.status_code >= 300:
                raise MCPOAuthError("MCP_OAUTH_UPSTREAM_REJECTED")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self._response_limit_bytes:
                        raise MCPOAuthError("MCP_OAUTH_RESPONSE_TOO_LARGE")
                except ValueError as exc:
                    raise MCPOAuthError("MCP_OAUTH_RESPONSE_INVALID") from exc
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self._response_limit_bytes:
                    raise MCPOAuthError("MCP_OAUTH_RESPONSE_TOO_LARGE")
                body.extend(chunk)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                raise MCPOAuthError("MCP_OAUTH_CONTENT_TYPE_INVALID")
        except httpx.TimeoutException as exc:
            raise MCPOAuthError("MCP_OAUTH_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise MCPOAuthError("MCP_OAUTH_UPSTREAM_UNAVAILABLE") from exc
        finally:
            if response is not None:
                await response.aclose()
            if owns_request_client:
                await http.aclose()
        try:
            payload = json.loads(bytes(body))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise MCPOAuthError("MCP_OAUTH_RESPONSE_INVALID") from exc
        if not isinstance(payload, dict):
            raise MCPOAuthError("MCP_OAUTH_RESPONSE_INVALID")
        return payload

    async def begin(
        self,
        *,
        tenant_id: str,
        user_id: str,
        server_id: str,
        principal_type: str,
        owner_user_id: str | None,
        metadata_url: str,
        resource: str,
        audience: str,
        client_id: str,
        redirect_uri: str,
        scopes: Iterable[str],
    ) -> dict[str, str]:
        if principal_type not in {"service_account", "user_delegated"}:
            raise MCPOAuthError("MCP_OAUTH_PRINCIPAL_INVALID")
        if principal_type == "service_account" and owner_user_id is not None:
            raise MCPOAuthError("MCP_OAUTH_PRINCIPAL_INVALID")
        if principal_type == "user_delegated" and owner_user_id != user_id:
            raise MCPOAuthError("MCP_OAUTH_PRINCIPAL_INVALID")
        if not all((tenant_id, user_id, server_id, client_id, resource, audience)):
            raise MCPOAuthError("MCP_OAUTH_REQUEST_INVALID")
        self._validate_endpoint(resource)
        self._validate_endpoint(redirect_uri)
        metadata = await self._request_json("GET", metadata_url)
        if metadata.get("resource") != resource or (
            "audience" in metadata and not _exact_audience(metadata.get("audience"), audience)
        ):
            raise MCPOAuthError("MCP_OAUTH_RESOURCE_MISMATCH")
        authorization_endpoint = str(metadata.get("authorization_endpoint") or "")
        token_endpoint = str(metadata.get("token_endpoint") or "")
        issuer = str(metadata.get("issuer") or "")
        if not all((authorization_endpoint, token_endpoint, issuer)):
            raise MCPOAuthError("MCP_OAUTH_METADATA_INVALID")
        self._validate_endpoint(issuer)
        self._validate_endpoint(authorization_endpoint)
        self._validate_endpoint(token_endpoint)
        if _origin(authorization_endpoint) != _origin(issuer) or _origin(token_endpoint) != _origin(
            issuer
        ):
            raise MCPOAuthError("MCP_OAUTH_ISSUER_MISMATCH")
        methods = metadata.get("code_challenge_methods_supported") or []
        if "S256" not in methods:
            raise MCPOAuthError("MCP_OAUTH_PKCE_REQUIRED")
        requested_scopes = tuple(sorted({str(scope) for scope in scopes if str(scope)}))
        supported_scopes = metadata.get("scopes_supported")
        if isinstance(supported_scopes, list) and not set(requested_scopes).issubset(
            {str(scope) for scope in supported_scopes}
        ):
            raise MCPOAuthError("MCP_OAUTH_SCOPE_DENIED")

        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        session = MCPOAuthSession(
            state=state,
            tenant_id=tenant_id,
            user_id=user_id,
            server_id=server_id,
            principal_type=principal_type,
            owner_user_id=owner_user_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
            token_endpoint=token_endpoint,
            resource=resource,
            audience=audience,
            scopes=requested_scopes,
            code_verifier=verifier,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=_SESSION_TTL_SECONDS),
        )
        await self._session_store.save(session)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": resource,
                "audience": audience,
                "scope": " ".join(requested_scopes),
            }
        )
        separator = "&" if urllib.parse.urlsplit(authorization_endpoint).query else "?"
        return {
            "authorization_url": f"{authorization_endpoint}{separator}{query}",
            "state": state,
        }

    async def complete(
        self,
        *,
        state: str,
        code: str,
        tenant_id: str,
        user_id: str,
        server_id: str,
        principal_type: str,
    ) -> MCPOAuthGrant:
        session = await self._session_store.consume(state)
        if not code or not all(
            (
                secrets.compare_digest(session.tenant_id, tenant_id),
                secrets.compare_digest(session.user_id, user_id),
                secrets.compare_digest(session.server_id, server_id),
                secrets.compare_digest(session.principal_type, principal_type),
            )
        ):
            raise MCPOAuthError("MCP_OAUTH_STATE_IDENTITY_MISMATCH")
        payload = await self._request_json(
            "POST",
            session.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": session.client_id,
                "redirect_uri": session.redirect_uri,
                "code_verifier": session.code_verifier,
                "resource": session.resource,
                "audience": session.audience,
            },
        )
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not access_token
            or payload.get("token_type", "").lower() != "bearer"
        ):
            raise MCPOAuthError("MCP_OAUTH_TOKEN_INVALID")
        if refresh_token is not None and not isinstance(refresh_token, str):
            raise MCPOAuthError("MCP_OAUTH_TOKEN_INVALID")
        token_audience = payload.get("audience", payload.get("resource"))
        if not _exact_audience(token_audience, session.audience):
            raise MCPOAuthError("MCP_OAUTH_AUDIENCE_MISMATCH")
        raw_scope = payload.get("scope")
        granted_scopes = (
            tuple(sorted(set(str(raw_scope).split()))) if raw_scope is not None else session.scopes
        )
        if not set(granted_scopes).issubset(session.scopes):
            raise MCPOAuthError("MCP_OAUTH_SCOPE_ELEVATION")
        try:
            expires_in = int(payload.get("expires_in"))
        except (TypeError, ValueError) as exc:
            raise MCPOAuthError("MCP_OAUTH_TOKEN_INVALID") from exc
        if expires_in < 1 or expires_in > 31 * 24 * 60 * 60:
            raise MCPOAuthError("MCP_OAUTH_TOKEN_INVALID")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        secret_ref = await self._secret_writer.put_oauth_credential(
            tenant_id=tenant_id,
            server_id=server_id,
            owner_user_id=session.owner_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            audience=session.audience,
            scopes=granted_scopes,
            expires_at=expires_at,
        )
        return MCPOAuthGrant(
            secret_ref=secret_ref,
            scopes=granted_scopes,
            audience=session.audience,
            expires_at=expires_at,
            refresh_configured=bool(refresh_token),
        )


__all__ = [
    "InMemoryMCPOAuthSecretStore",
    "InMemoryMCPOAuthSessionStore",
    "MCPOAuthCoordinator",
    "MCPOAuthError",
    "MCPOAuthGrant",
    "MCPOAuthSecretWriter",
    "MCPOAuthSession",
    "MCPOAuthSessionStore",
]
