"""Tenant MCP discovery, credential resolution and Agent runtime adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Callable
from typing import Any, Protocol

from ai_gateway_core.persistence.repositories.mcp_repository import (
    MCPAuthorizationError,
)

from ..tools.tool_registry import (
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
)
from .client import MCPClient, MCPError, MCPServerConfig

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class MCPSecretUnavailable(RuntimeError):
    """An opaque reference cannot be resolved by the configured store."""


class MCPSecretResolver(Protocol):
    async def resolve(self, secret_ref: str) -> str: ...


class MappingSecretResolver:
    """Test/local Secret Store adapter whose values are never exposed in repr."""

    def __init__(self, values: dict[str, str] | None = None):
        self._values = dict(values or {})

    async def resolve(self, secret_ref: str) -> str:
        value = self._values.get(secret_ref)
        if not value:
            raise MCPSecretUnavailable("MCP_SECRET_UNAVAILABLE")
        return value

    def __repr__(self) -> str:
        return f"MappingSecretResolver(refs={len(self._values)})"


class ConfiguredEnvironmentSecretResolver:
    """Resolve only operator-allowlisted references to environment variables.

    `MCP_SECRET_REF_MAP` contains reference-to-variable-name metadata, never the
    secret value itself. A tenant cannot use `env://JWT_SECRET` unless an
    operator explicitly added that exact reference to the map.
    """

    def __init__(self, reference_map: dict[str, str]):
        self._reference_map = {
            str(reference): str(env_name)
            for reference, env_name in reference_map.items()
            if isinstance(reference, str)
            and isinstance(env_name, str)
            and _ENV_NAME_RE.fullmatch(env_name)
        }

    @classmethod
    def from_env(cls) -> ConfiguredEnvironmentSecretResolver:
        raw = os.getenv("MCP_SECRET_REF_MAP", "").strip()
        if not raw:
            return cls({})
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return cls({})
        return cls(value if isinstance(value, dict) else {})

    async def resolve(self, secret_ref: str) -> str:
        env_name = self._reference_map.get(secret_ref)
        value = os.getenv(env_name, "") if env_name else ""
        if not value:
            raise MCPSecretUnavailable("MCP_SECRET_UNAVAILABLE")
        return value

    def __repr__(self) -> str:
        return f"ConfiguredEnvironmentSecretResolver(refs={len(self._reference_map)})"


MCPClientFactory = Callable[[MCPServerConfig], MCPClient]


def _default_client_factory(config: MCPServerConfig) -> MCPClient:
    return MCPClient(config)


def _config_from_authorization(
    item: dict[str, Any],
    *,
    credential: str | None,
) -> MCPServerConfig:
    origins = [str(value) for value in (item.get("allowed_origins") or [])]
    return MCPServerConfig(
        name=str(item.get("name") or item.get("server_id") or "mcp"),
        url=str(item["base_url"]),
        api_key=credential,
        transport="streamable_http",
        timeout=max(0.1, int(item.get("timeout_ms") or 30000) / 1000),
        max_concurrent=int(item.get("max_concurrency") or 5),
        response_limit_bytes=int(item.get("response_limit_bytes") or 1048576),
        auth_method=str(item.get("auth_method") or "none"),
        oauth_resource=item.get("oauth_resource"),
        oauth_audience=item.get("oauth_audience"),
        credential_audience=item.get("audience"),
        origin=origins[0] if origins else None,
        allowed_origins=origins,
        platform_managed=False,
        allow_localhost=False,
        allow_private_network=False,
    )


async def _resolved_credential(
    item: dict[str, Any],
    resolver: MCPSecretResolver,
) -> str | None:
    if item.get("auth_method") == "none":
        return None
    secret_ref = str(item.get("secret_ref") or "")
    if not secret_ref:
        raise MCPSecretUnavailable("MCP_SECRET_UNAVAILABLE")
    return await resolver.resolve(secret_ref)


class MCPDiscoveryService:
    """Discover one explicit connection and persist immutable snapshots."""

    def __init__(
        self,
        *,
        secret_resolver: MCPSecretResolver,
        client_factory: MCPClientFactory = _default_client_factory,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._client_factory = client_factory

    async def discover(
        self,
        *,
        tenant_id: str,
        user_id: str,
        server_id: str,
        connection_id: str,
        principal_type: str,
        repository: Any,
    ) -> dict[str, Any]:
        item = await repository.resolve_discovery_connection(
            tenant_id=tenant_id,
            user_id=user_id,
            authenticated=True,
            server_id=server_id,
            connection_id=connection_id,
            principal_type=principal_type,
        )
        credential = await _resolved_credential(item, self._secret_resolver)
        client = self._client_factory(_config_from_authorization(item, credential=credential))
        try:
            await client.initialize()
            tools = await client.list_tools()
        except MCPError as exc:
            with contextlib.suppress(Exception):
                await repository.record_runtime_result(
                    tenant_id=tenant_id,
                    server_id=server_id,
                    success=False,
                    error_code=exc.stable_code,
                )
            raise
        finally:
            await client.close()
        return await repository.record_discovery(
            tenant_id=tenant_id,
            server_id=server_id,
            tools=[
                {
                    "name": tool.upstream_name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                    # MCP annotations are remote-controlled hints, not a
                    # platform trust signal. Public read-only eligibility is
                    # established only by an exact-schema Tenant Admin grant.
                    "read_only": False,
                    "risk_level": "medium",
                }
                for tool in tools
            ],
        )


class MCPRuntimeService:
    """Dynamic MCP definitions/invocation behind the AS-02 allowlist."""

    def __init__(
        self,
        *,
        repository: Any,
        secret_resolver: MCPSecretResolver,
        client_factory: MCPClientFactory = _default_client_factory,
    ) -> None:
        self._repository = repository
        self._secret_resolver = secret_resolver
        self._client_factory = client_factory
        self._connection_semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}

    def _connection_semaphore(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        max_concurrency: int,
    ) -> asyncio.Semaphore:
        limit = max(1, min(32, max_concurrency))
        # Key by tenant:connection (not by limit) so the cache stays bounded as
        # max_concurrency changes, and rebuild the semaphore when the configured
        # limit changes so new requests honor the current cap (AS-MCP-007).
        key = f"{tenant_id}:{connection_id}"
        entry = self._connection_semaphores.get(key)
        if entry is None or entry[0] != limit:
            entry = (limit, asyncio.Semaphore(limit))
            self._connection_semaphores[key] = entry
        return entry[1]

    @staticmethod
    def _identity(context: Any) -> tuple[str, str, bool, str]:
        user = getattr(context, "user", None)
        authenticated = bool(getattr(user, "is_authenticated", False))
        metadata = getattr(context, "metadata", None) or {}
        channel = str(metadata.get("channel") or "")
        if channel == "hosted":
            # The Agent Runtime envelope signs the raw delivery channel
            # ("hosted"), but the MCP/connector authorization layer uses a
            # finer vocabulary ({hosted_private, hosted_public}). Normalize
            # on the publication auth_mode (the publication's exposure, not
            # merely whether this caller authenticated) so hosted MCP and
            # connector tools authorize instead of failing closed, and the
            # public read-only grant guard applies to genuinely public,
            # anonymous hosted exposure. preview/embed/api are already valid
            # members of MCP_CHANNELS and pass through unchanged; "builtin"
            # is not an MCP delivery channel and stays denied downstream.
            auth_mode = str(metadata.get("publication_auth_mode") or "")
            channel = "hosted_public" if auth_mode == "public" else "hosted_private"
        return (
            str(getattr(context, "tenant_id", "") or ""),
            str(getattr(context, "user_id", "") or ""),
            authenticated,
            channel,
        )

    async def authorize_binding(
        self,
        *,
        tool_name: str,
        binding: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        tenant_id, user_id, authenticated, channel = self._identity(context)
        config = binding.get("config")
        config = config if isinstance(config, dict) else {}
        if str(binding.get("type") or binding.get("capability_type") or "") != "mcp":
            raise MCPAuthorizationError("MCP_CAPABILITY_TYPE_INVALID")
        if str(binding.get("id") or binding.get("resource_id") or "") != tool_name:
            raise MCPAuthorizationError("MCP_CAPABILITY_NOT_BOUND")
        return await self._repository.authorize_mcp_tool(
            tenant_id=tenant_id,
            user_id=user_id,
            authenticated=authenticated,
            runtime_name=tool_name,
            schema_hash=str(binding.get("schema_hash") or ""),
            risk_level=str(
                binding.get("risk") or binding.get("risk_level") or config.get("risk") or ""
            ),
            connection_id=str(config.get("connection_id") or ""),
            principal_type=str(config.get("principal_type") or ""),
            channel=channel,
        )

    async def authorize_connector_binding(
        self,
        *,
        tool_name: str,
        binding: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        tenant_id, user_id, authenticated, channel = self._identity(context)
        config = binding.get("config")
        config = config if isinstance(config, dict) else {}
        if str(binding.get("type") or binding.get("capability_type") or "") != "connector":
            raise MCPAuthorizationError("CONNECTOR_CAPABILITY_TYPE_INVALID")
        if str(binding.get("id") or binding.get("resource_id") or "") != tool_name:
            raise MCPAuthorizationError("CONNECTOR_CAPABILITY_NOT_BOUND")
        if str(config.get("tool_name") or tool_name) != tool_name:
            raise MCPAuthorizationError("CONNECTOR_CAPABILITY_NOT_BOUND")
        return await self._repository.authorize_connector_tool(
            tenant_id=tenant_id,
            user_id=user_id,
            authenticated=authenticated,
            provider=str(config.get("provider") or ""),
            tool_name=tool_name,
            principal_type=str(config.get("principal_type") or ""),
            grant_id=str(config.get("grant_id") or ""),
            channel=channel,
        )

    @staticmethod
    def _definition(item: dict[str, Any]) -> ToolDefinition:
        schema = item.get("input_schema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        parameters = [
            ToolParameter(
                name=str(name),
                type=str(value.get("type") or "string"),
                description=MCPClient._sanitize_description(str(value.get("description") or "")),
                required=name in required,
            )
            for name, value in properties.items()
            if isinstance(value, dict)
        ]
        raw_risk = str(item.get("risk_level") or "medium")
        risk = ToolRiskLevel.HIGH if raw_risk == "critical" else ToolRiskLevel(raw_risk)
        definition = ToolDefinition(
            name=str(item["runtime_name"]),
            description=MCPClient._sanitize_description(str(item.get("description") or "")),
            parameters=parameters,
            category=ToolCategory.MCP,
            risk_level=risk,
            when_to_use="Only when this exact Agent Version binding requires it.",
            when_not_to_use="When the connection, schema, caller, channel or server is unavailable.",
            timeout_seconds=max(1, int(item.get("timeout_ms") or 30000) // 1000),
            is_async=True,
        )
        definition.capability_metadata = {
            "kind": "mcp",
            "server_id": str(item["server_id"]),
            "tool_id": str(item["tool_id"]),
            "schema_hash": str(item["schema_hash"]),
            "schema_version": int(item["schema_version"]),
            "setup_state": "ready",
            "health": str(item.get("health_status") or "unknown"),
            "read_only": bool(item.get("read_only", False)),
            "external_service": True,
        }
        return definition

    async def get_tool_definitions(
        self,
        *,
        context: Any,
        bindings: dict[str, dict[str, Any]],
        tool_names: set[str],
    ) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for tool_name in sorted(tool_names):
            binding = bindings.get(tool_name)
            if not binding or str(binding.get("type") or "") != "mcp":
                continue
            try:
                item = await self.authorize_binding(
                    tool_name=tool_name,
                    binding=binding,
                    context=context,
                )
            except (MCPAuthorizationError, MCPSecretUnavailable):
                continue
            definitions.append(self._definition(item))
        return definitions

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        binding: dict[str, Any],
        context: Any,
        call_id: str,
    ) -> ToolCallResult:
        server_id = ""
        try:
            item = await self.authorize_binding(
                tool_name=tool_name,
                binding=binding,
                context=context,
            )
            server_id = str(item["server_id"])
            credential = await _resolved_credential(item, self._secret_resolver)
            config = _config_from_authorization(item, credential=credential)
            tenant_id = str(getattr(context, "tenant_id", ""))
            semaphore = self._connection_semaphore(
                tenant_id=tenant_id,
                connection_id=str(item["connection_id"]),
                max_concurrency=config.max_concurrent,
            )
            async with asyncio.timeout(config.timeout):
                async with semaphore:
                    client = self._client_factory(config)
                    try:
                        await client.initialize()
                        result = await client.call_tool(str(item["upstream_name"]), arguments)
                    finally:
                        await client.close()
            await self._repository.record_runtime_result(
                tenant_id=str(getattr(context, "tenant_id", "")),
                server_id=server_id,
                success=not result.is_error,
                error_code="MCP_REMOTE_TOOL_ERROR" if result.is_error else None,
            )
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=not result.is_error,
                result=result.content if not result.is_error else None,
                error="MCP tool failed" if result.is_error else None,
                metadata={
                    "mcp_server_id": server_id,
                    "mcp_tool_id": str(item["tool_id"]),
                    "mcp_schema_hash": str(item["schema_hash"]),
                },
            )
        except MCPAuthorizationError as exc:
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error=exc.code,
            )
        except MCPSecretUnavailable:
            error_code = "MCP_SECRET_UNAVAILABLE"
        except TimeoutError:
            error_code = "MCP_TIMEOUT"
        except MCPError as exc:
            error_code = exc.stable_code
        except Exception:
            error_code = "MCP_UPSTREAM_UNAVAILABLE"
        if server_id:
            with contextlib.suppress(Exception):
                await self._repository.record_runtime_result(
                    tenant_id=str(getattr(context, "tenant_id", "")),
                    server_id=server_id,
                    success=False,
                    error_code=error_code,
                )
        return ToolCallResult(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            error=error_code,
        )


__all__ = [
    "ConfiguredEnvironmentSecretResolver",
    "MCPDiscoveryService",
    "MCPRuntimeService",
    "MCPSecretResolver",
    "MCPSecretUnavailable",
    "MappingSecretResolver",
]
