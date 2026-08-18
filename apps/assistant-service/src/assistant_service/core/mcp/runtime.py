"""Tenant MCP discovery, credential resolution and Agent runtime adapter."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ai_gateway_core.logging import get_logger, record_internal_exception
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
from .client import MCP_PROTOCOL_VERSION, MCPClient, MCPError, MCPServerConfig
from .resilience import (
    MCPCircuitBreaker,
    MCPCircuitLease,
    MCPCircuitOpen,
    MCPFailureKind,
    MCPInvocationPolicy,
    MCPOperationKind,
    build_operation_identity,
    counts_toward_circuit,
    decide_mcp_failure,
)

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
logger = get_logger(__name__)


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
MCPClientCacheKey = tuple[str, str, str, str]


@dataclass
class _CachedMCPClient:
    key: MCPClientCacheKey
    client: Any
    config_fingerprint: str
    pin_revision: str
    expires_at: float
    last_used_at: float
    in_use: int = 0
    invalidated: bool = False


def _default_client_factory(config: MCPServerConfig) -> MCPClient:
    return MCPClient(config)


def _config_from_authorization(
    item: dict[str, Any],
    *,
    credential: str | None,
) -> MCPServerConfig:
    origins = [str(value) for value in (item.get("allowed_origins") or [])]
    host_deadline = max(0.1, int(item.get("timeout_ms") or 30000) / 1000)
    transport_timeout = max(
        0.1,
        int(item.get("transport_timeout_ms") or item.get("timeout_ms") or 30000) / 1000,
    )
    return MCPServerConfig(
        name=str(item.get("name") or item.get("server_id") or "mcp"),
        url=str(item["base_url"]),
        api_key=credential,
        transport="streamable_http",
        timeout=transport_timeout,
        host_deadline=host_deadline,
        max_concurrent=int(item.get("max_concurrency") or 5),
        circuit_failure_threshold=int(item.get("circuit_failure_threshold") or 3),
        circuit_cooldown_seconds=float(
            30
            if item.get("circuit_cooldown_seconds") is None
            else item.get("circuit_cooldown_seconds")
        ),
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
            try:
                await repository.record_runtime_result(
                    tenant_id=tenant_id,
                    server_id=server_id,
                    success=False,
                    error_code=exc.stable_code,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__,
                    "assistant.core.mcp.runtime.suppressed_failure",
                    exc,
                    level=logging.DEBUG,
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
        circuit_clock: Callable[[], float] = time.monotonic,
        client_cache_ttl_seconds: float | None = None,
        client_cache_max_entries: int | None = None,
    ) -> None:
        self._repository = repository
        self._secret_resolver = secret_resolver
        self._client_factory = client_factory
        self._circuit_clock = circuit_clock
        self._connection_semaphores: dict[str, tuple[int, asyncio.Semaphore]] = {}
        self._connection_breakers: dict[
            str,
            tuple[int, float, MCPCircuitBreaker],
        ] = {}
        self._client_cache_ttl_seconds = max(
            1.0,
            min(
                3600.0,
                float(
                    client_cache_ttl_seconds
                    if client_cache_ttl_seconds is not None
                    else 60
                ),
            ),
        )
        self._client_cache_max_entries = max(
            1,
            min(
                1000,
                int(
                    client_cache_max_entries
                    if client_cache_max_entries is not None
                    else 100
                ),
            ),
        )
        self._client_cache: dict[MCPClientCacheKey, _CachedMCPClient] = {}
        self._client_initializers: dict[
            MCPClientCacheKey,
            asyncio.Task[_CachedMCPClient],
        ] = {}
        self._client_cache_lock = asyncio.Lock()

    @staticmethod
    def _credential_revision(item: dict[str, Any], credential: str | None) -> str:
        explicit = item.get("credential_revision") or item.get("credential_updated_at")
        if explicit is not None:
            return str(explicit)
        if credential is None:
            return "none"
        return "sha256:" + hashlib.sha256(credential.encode("utf-8")).hexdigest()

    @staticmethod
    def _client_config_fingerprint(item: dict[str, Any], config: MCPServerConfig) -> str:
        values = {
            "server_revision": str(item.get("server_updated_at") or ""),
            "url": config.url,
            "transport": config.transport,
            "timeout": config.timeout,
            "host_deadline": config.host_deadline,
            "max_concurrent": config.max_concurrent,
            "response_limit_bytes": config.response_limit_bytes,
            "auth_method": config.auth_method,
            "oauth_resource": config.oauth_resource,
            "oauth_audience": config.oauth_audience,
            "credential_audience": config.credential_audience,
            "origin": config.origin,
            "allowed_origins": sorted(config.allowed_origins),
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _pin_revision(item: dict[str, Any]) -> str:
        return str(item.get("dns_pin_revision") or item.get("pin_revision") or "")

    async def _initialize_cached_client(
        self,
        *,
        key: MCPClientCacheKey,
        config: MCPServerConfig,
        config_fingerprint: str,
        pin_revision: str,
    ) -> _CachedMCPClient:
        client = self._client_factory(config)
        try:
            await client.initialize()
        except BaseException:
            try:
                await client.close()
            except Exception as exc:
                record_internal_exception(
                    __name__, "mcp.runtime.client_initialize_cleanup_failed", exc
                )
            raise
        now = self._circuit_clock()
        return _CachedMCPClient(
            key=key,
            client=client,
            config_fingerprint=config_fingerprint,
            pin_revision=pin_revision,
            expires_at=now + self._client_cache_ttl_seconds,
            last_used_at=now,
        )

    @staticmethod
    async def _close_clients(clients: list[Any]) -> None:
        for client in clients:
            try:
                await client.close()
            except Exception as exc:
                record_internal_exception(__name__, "mcp.runtime.client_close_failed", exc)

    async def _acquire_cached_client(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        credential_revision: str,
        config: MCPServerConfig,
        config_fingerprint: str,
        pin_revision: str,
    ) -> _CachedMCPClient:
        key = (tenant_id, connection_id, credential_revision, MCP_PROTOCOL_VERSION)
        now = self._circuit_clock()
        close_now: list[Any] = []
        entry: _CachedMCPClient | None = None
        async with self._client_cache_lock:
            for cached_key, cached in list(self._client_cache.items()):
                same_connection = cached_key[:2] == key[:2]
                mismatched = same_connection and (
                    cached_key != key
                    or cached.config_fingerprint != config_fingerprint
                    or cached.pin_revision != pin_revision
                )
                if cached.expires_at <= now or mismatched:
                    self._client_cache.pop(cached_key, None)
                    cached.invalidated = True
                    if cached.in_use == 0:
                        close_now.append(cached.client)

            entry = self._client_cache.get(key)
            if entry is not None:
                entry.in_use += 1
                entry.last_used_at = now
                initializer = None
            else:
                initializer = self._client_initializers.get(key)
                if initializer is None:
                    initializer = asyncio.create_task(
                        self._initialize_cached_client(
                            key=key,
                            config=config,
                            config_fingerprint=config_fingerprint,
                            pin_revision=pin_revision,
                        )
                    )
                    self._client_initializers[key] = initializer

        await self._close_clients(close_now)
        if entry is not None:
            return entry

        assert initializer is not None
        try:
            initialized = await asyncio.shield(initializer)
        except asyncio.CancelledError:
            # Keep the singleflight task discoverable. A later caller can
            # adopt the initialized client, and shutdown will cancel/close it.
            raise
        except BaseException:
            async with self._client_cache_lock:
                if self._client_initializers.get(key) is initializer:
                    self._client_initializers.pop(key, None)
            raise

        evicted: list[Any] = []
        async with self._client_cache_lock:
            if self._client_initializers.get(key) is initializer:
                self._client_initializers.pop(key, None)
            entry = self._client_cache.get(key)
            if entry is None:
                entry = initialized
                self._client_cache[key] = entry
            entry.in_use += 1
            entry.last_used_at = self._circuit_clock()
            while len(self._client_cache) > self._client_cache_max_entries:
                candidate = min(
                    (
                        cached
                        for cached in self._client_cache.values()
                        if cached.in_use == 0 and cached is not entry
                    ),
                    key=lambda cached: cached.last_used_at,
                    default=None,
                )
                if candidate is None:
                    break
                self._client_cache.pop(candidate.key, None)
                candidate.invalidated = True
                evicted.append(candidate.client)
        await self._close_clients(evicted)
        return entry

    async def _release_cached_client(
        self,
        entry: _CachedMCPClient,
        *,
        invalidate: bool,
    ) -> None:
        close_clients: list[Any] = []
        async with self._client_cache_lock:
            entry.in_use = max(0, entry.in_use - 1)
            entry.last_used_at = self._circuit_clock()
            expired = entry.expires_at <= self._circuit_clock()
            if invalidate or expired:
                if self._client_cache.get(entry.key) is entry:
                    self._client_cache.pop(entry.key, None)
                entry.invalidated = True
            if entry.invalidated and entry.in_use == 0:
                close_clients.append(entry.client)
            while len(self._client_cache) > self._client_cache_max_entries:
                candidate = min(
                    (cached for cached in self._client_cache.values() if cached.in_use == 0),
                    key=lambda cached: cached.last_used_at,
                    default=None,
                )
                if candidate is None:
                    break
                self._client_cache.pop(candidate.key, None)
                candidate.invalidated = True
                close_clients.append(candidate.client)
        await self._close_clients(close_clients)

    @staticmethod
    def _invalidates_cached_client(exc: MCPError) -> bool:
        return exc.code == 404 or exc.stable_code in {
            "MCP_SESSION_INVALID",
            "MCP_SESSION_CONFUSION_BLOCKED",
            "MCP_DNS_REBINDING_BLOCKED",
        }

    async def close(self) -> None:
        """Close all initialized dynamic clients during service shutdown."""
        async with self._client_cache_lock:
            initializers = list(self._client_initializers.values())
            self._client_initializers.clear()
            clients = [entry.client for entry in self._client_cache.values()]
            self._client_cache.clear()
        for task in initializers:
            task.cancel()
        if initializers:
            results = await asyncio.gather(*initializers, return_exceptions=True)
            clients.extend(
                result.client for result in results if isinstance(result, _CachedMCPClient)
            )
        await self._close_clients(clients)

    async def _record_runtime_result_best_effort(
        self,
        *,
        tenant_id: str,
        server_id: str,
        success: bool,
        error_code: str | None,
        count_failure: bool = True,
    ) -> None:
        """Bound telemetry so it cannot hide a completed operation outcome."""

        task = asyncio.create_task(
            self._repository.record_runtime_result(
                tenant_id=tenant_id,
                server_id=server_id,
                success=success,
                error_code=error_code,
                counts_toward_circuit=count_failure,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        except TimeoutError as exc:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_exc:
                record_internal_exception(
                    __name__,
                    "mcp.runtime.telemetry_cancel_cleanup_failed",
                    cleanup_exc,
                    level=logging.DEBUG,
                )
            record_internal_exception(
                __name__,
                "mcp.runtime.telemetry_record_timeout",
                exc,
                level=logging.WARNING,
            )
        except asyncio.CancelledError:
            # Upstream already produced an authoritative result. A caller
            # cancellation while optional telemetry is pending must not turn a
            # known success into an ambiguous write outcome.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as cleanup_exc:
                record_internal_exception(
                    __name__,
                    "mcp.runtime.telemetry_cancel_cleanup_failed",
                    cleanup_exc,
                    level=logging.DEBUG,
                )
        except Exception as exc:
            record_internal_exception(__name__, "mcp.runtime.telemetry_record_failed", exc)

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

    def _connection_breaker(
        self,
        *,
        tenant_id: str,
        connection_id: str,
        failure_threshold: int,
        cooldown_seconds: float,
    ) -> MCPCircuitBreaker:
        """Return a tenant/connection-scoped breaker with bounded retention."""

        threshold = max(1, min(20, int(failure_threshold)))
        cooldown = max(0.0, min(3600.0, float(cooldown_seconds)))
        key = f"{tenant_id}:{connection_id}"
        entry = self._connection_breakers.get(key)
        if entry is None or entry[:2] != (threshold, cooldown):
            entry = (
                threshold,
                cooldown,
                MCPCircuitBreaker(
                    failure_threshold=threshold,
                    cooldown_seconds=cooldown,
                    clock=self._circuit_clock,
                ),
            )
            self._connection_breakers[key] = entry
        if len(self._connection_breakers) > 500:
            oldest = min(
                self._connection_breakers,
                key=lambda item: self._connection_breakers[item][2].touched_at,
            )
            if oldest != key:
                self._connection_breakers.pop(oldest, None)
        return entry[2]

    @staticmethod
    def _invocation_policy(
        *,
        item: dict[str, Any],
        binding: dict[str, Any],
        context: Any,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
    ) -> MCPInvocationPolicy:
        # The repository authorization result is the trusted source for
        # operation semantics. Agent-version bindings are signed capability
        # selectors, but their free-form config must not be able to assert
        # idempotency or name an executable recovery tool.
        del binding
        operation_id, idempotency_key = build_operation_identity(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            logical_operation_id=str(
                (getattr(context, "metadata", None) or {}).get("logical_operation_id") or call_id
            ),
        )
        read_only = bool(item.get("read_only", False))
        idempotency_supported = bool(item.get("idempotency_supported"))
        read_back_tool = str(item.get("read_back_tool") or "") or None
        read_back_argument = str(item.get("read_back_argument") or "operation_id")
        compensation_available = bool(item.get("compensation_available"))
        max_retries = max(0, min(2, int(getattr(context, "max_retries", 0) or 0)))
        safe_to_retry = read_only or idempotency_supported
        return MCPInvocationPolicy(
            operation_kind=(MCPOperationKind.READ if read_only else MCPOperationKind.WRITE),
            operation_id=operation_id,
            circuit_scope=":".join(
                [
                    str(getattr(context, "tenant_id", "") or "unresolved"),
                    str(item.get("connection_id") or "unresolved"),
                ]
            ),
            idempotency_key=idempotency_key if idempotency_supported else None,
            idempotency_supported=idempotency_supported,
            read_back_tool=read_back_tool,
            read_back_argument=read_back_argument,
            compensation_available=compensation_available,
            max_attempts=1 + max_retries if safe_to_retry else 1,
        )

    @staticmethod
    async def _call_client_tool(
        client: Any,
        tool_name: str,
        arguments: dict[str, Any],
        policy: MCPInvocationPolicy,
    ) -> Any:
        """Use the additive policy keyword when a legacy client supports it."""

        try:
            parameters = inspect.signature(client.call_tool).parameters.values()
            supports_policy = any(
                parameter.name == "invocation_policy"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_policy = False
        if supports_policy:
            return await client.call_tool(
                tool_name,
                arguments,
                invocation_policy=policy,
            )
        return await client.call_tool(tool_name, arguments)

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
        provider = str(config.get("provider") or "")
        principal_type = str(config.get("principal_type") or "")
        grant_id = str(config.get("grant_id") or "")
        if principal_type or grant_id:
            if not principal_type or not grant_id:
                raise MCPAuthorizationError("CONNECTOR_BINDING_INCOMPLETE")
            return await self._repository.authorize_connector_tool(
                tenant_id=tenant_id,
                user_id=user_id,
                authenticated=authenticated,
                provider=provider,
                tool_name=tool_name,
                principal_type=principal_type,
                grant_id=grant_id,
                channel=channel,
            )
        return await self._repository.authorize_connector_catalog(
            tenant_id=tenant_id,
            user_id=user_id,
            authenticated=authenticated,
            provider=provider,
            tool_name=tool_name,
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
        public_read_grant = bool(item.get("admin_read_only_approved") and item.get("read_only"))
        definition = ToolDefinition(
            name=str(item["runtime_name"]),
            description=MCPClient._sanitize_description(str(item.get("description") or "")),
            parameters=parameters,
            category=ToolCategory.MCP,
            risk_level=risk,
            requires_confirmation=(
                not public_read_grant
                and (
                    not bool(item.get("read_only", False))
                    or risk in {ToolRiskLevel.MEDIUM, ToolRiskLevel.HIGH}
                )
            ),
            when_to_use="Only when this exact Agent Version binding requires it.",
            when_not_to_use=(
                "When the connection, schema, caller, channel or server is unavailable."
            ),
            timeout_seconds=max(1, int(item.get("timeout_ms") or 30000) // 1000),
            is_async=True,
            argument_schema=schema,
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
            "operation_kind": ("read" if bool(item.get("read_only", False)) else "write"),
            "idempotency_supported": bool(item.get("idempotency_supported", False)),
            "read_back_available": bool(item.get("read_back_tool")),
            "compensation_available": bool(item.get("compensation_available", False)),
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
            except Exception as exc:
                record_internal_exception(
                    __name__, "mcp.runtime.authorization_catalog_failed_closed", exc
                )
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
        breaker: MCPCircuitBreaker | None = None
        lease: MCPCircuitLease | None = None
        policy = MCPInvocationPolicy(operation_id="")
        circuit: dict[str, Any] = {}
        recovery_evidence: dict[str, Any] = {}
        deadline_scope: str | None = None
        operation_started = False
        circuit_recorded = False
        try:
            try:
                item = await self.authorize_binding(
                    tool_name=tool_name,
                    binding=binding,
                    context=context,
                )
            except MCPAuthorizationError:
                raise
            except Exception as exc:
                record_internal_exception(
                    __name__, "mcp.runtime.invocation_authorization_failed_closed", exc
                )
                failure = decide_mcp_failure(
                    "MCP_AUTHORIZATION_UNAVAILABLE",
                    policy,
                    operation_started=False,
                )
                return ToolCallResult(
                    call_id=call_id,
                    tool_name=tool_name,
                    success=False,
                    error="MCP_AUTHORIZATION_UNAVAILABLE",
                    metadata={"mcp_failure": failure.to_dict()},
                )
            server_id = str(item["server_id"])
            policy = self._invocation_policy(
                item=item,
                binding=binding,
                context=context,
                tool_name=tool_name,
                arguments=arguments,
                call_id=call_id,
            )
            raw_risk = str(item.get("risk_level") or "medium")
            _, _, _, channel = self._identity(context)
            public_read_grant = bool(
                channel in {"hosted_public", "embed"}
                and item.get("admin_read_only_approved")
                and item.get("read_only")
            )
            requires_confirmation = not public_read_grant and (
                not bool(item.get("read_only", False)) or raw_risk in {"medium", "high", "critical"}
            )
            approval_consumed = bool(
                (getattr(context, "metadata", None) or {}).get("approval_consumed")
            )
            if requires_confirmation and not approval_consumed:
                failure = decide_mcp_failure("MCP_APPROVAL_REQUIRED", policy)
                return ToolCallResult(
                    call_id=call_id,
                    tool_name=tool_name,
                    success=False,
                    error="MCP_APPROVAL_REQUIRED",
                    metadata={
                        "mcp_operation": {
                            "operation_kind": policy.operation_kind.value,
                            "operation_id": policy.operation_id,
                            "idempotency_key_present": bool(policy.idempotency_key),
                            "idempotency_supported": policy.idempotency_supported,
                            "read_back_available": policy.read_back_available,
                            "compensation_available": policy.compensation_available,
                            "max_attempts": policy.max_attempts,
                        },
                        "mcp_failure": failure.to_dict(),
                    },
                )
            credential = await _resolved_credential(item, self._secret_resolver)
            config = _config_from_authorization(item, credential=credential)
            tenant_id = str(getattr(context, "tenant_id", ""))
            credential_revision = self._credential_revision(item, credential)
            config_fingerprint = self._client_config_fingerprint(item, config)
            pin_revision = self._pin_revision(item)
            semaphore = self._connection_semaphore(
                tenant_id=tenant_id,
                connection_id=str(item["connection_id"]),
                max_concurrency=config.max_concurrent,
            )
            breaker = self._connection_breaker(
                tenant_id=tenant_id,
                connection_id=str(item["connection_id"]),
                failure_threshold=config.circuit_failure_threshold,
                cooldown_seconds=config.circuit_cooldown_seconds,
            )
            host_timeout = asyncio.timeout(float(config.host_deadline or config.timeout))
            async with host_timeout, semaphore:
                # Acquire after capacity wait so queued calls observe the
                # latest open/half-open state. Circuit outcome is recorded
                # before this semaphore is released.
                lease = await breaker.acquire()
                client_entry: _CachedMCPClient | None = None
                invalidate_client = False
                try:
                    client_entry = await self._acquire_cached_client(
                        tenant_id=tenant_id,
                        connection_id=str(item["connection_id"]),
                        credential_revision=credential_revision,
                        config=config,
                        config_fingerprint=config_fingerprint,
                        pin_revision=pin_revision,
                    )
                    operation_started = True
                    try:
                        result = await self._call_client_tool(
                            client_entry.client,
                            str(item["upstream_name"]),
                            arguments,
                            policy,
                        )
                    except MCPError as exc:
                        invalidate_client = self._invalidates_cached_client(exc)
                        failure = exc.failure or decide_mcp_failure(
                            exc.stable_code,
                            policy,
                        )
                        exc.failure = failure
                        if failure.read_back_required and policy.read_back_tool:
                            # Do not execute the named read-back tool on the
                            # current client's authority. Recovery is a new
                            # exact tool invocation and must cross catalog,
                            # binding, policy, and approval checks itself.
                            recovery_evidence = {
                                "read_back_attempted": False,
                                "read_back_status": "pending_authorized_resume",
                                "read_back_tool": policy.read_back_tool,
                            }
                            exc.recovery_evidence = recovery_evidence
                        raise
                except MCPError as exc:
                    failure = exc.failure or decide_mcp_failure(
                        exc.stable_code,
                        policy,
                        operation_started=operation_started,
                    )
                    exc.failure = failure
                    if failure.cause is MCPFailureKind.CANCELLED:
                        await breaker.record_neutral(lease)
                    elif counts_toward_circuit(failure):
                        await breaker.record_failure(lease)
                    else:
                        await breaker.record_success(lease)
                    circuit_recorded = True
                    raise
                except asyncio.CancelledError:
                    if host_timeout.expired():
                        # Let asyncio.timeout translate its own cancellation
                        # into TimeoutError at the context boundary.
                        raise
                    error_code = (
                        "MCP_CANCELLED_AFTER_DISPATCH" if operation_started else "MCP_CANCELLED"
                    )
                    failure = decide_mcp_failure(
                        error_code,
                        policy,
                        operation_started=operation_started,
                    )
                    await breaker.record_neutral(lease)
                    circuit_recorded = True
                    raise MCPError(
                        499,
                        "MCP invocation cancelled",
                        stable_code=error_code,
                        failure=failure,
                    ) from None
                except Exception:
                    failure = decide_mcp_failure(
                        "MCP_UPSTREAM_UNAVAILABLE",
                        policy,
                        operation_started=operation_started,
                    )
                    if counts_toward_circuit(failure):
                        await breaker.record_failure(lease)
                    else:
                        await breaker.record_success(lease)
                    circuit_recorded = True
                    raise
                finally:
                    if client_entry is not None:
                        await self._release_cached_client(
                            client_entry,
                            invalidate=invalidate_client,
                        )
                await breaker.record_success(lease)
                circuit_recorded = True
            circuit = await breaker.snapshot()
            failure = result.failure or (
                decide_mcp_failure("MCP_REMOTE_TOOL_ERROR", policy) if result.is_error else None
            )
            await self._record_runtime_result_best_effort(
                tenant_id=str(getattr(context, "tenant_id", "")),
                server_id=server_id,
                success=not counts_toward_circuit(failure) if failure is not None else True,
                error_code="MCP_REMOTE_TOOL_ERROR" if result.is_error else None,
                count_failure=bool(failure is not None and counts_toward_circuit(failure)),
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
                    "mcp_operation": {
                        "operation_kind": policy.operation_kind.value,
                        "operation_id": policy.operation_id,
                        "idempotency_key_present": bool(policy.idempotency_key),
                        "idempotency_supported": policy.idempotency_supported,
                        "read_back_available": policy.read_back_available,
                        "compensation_available": policy.compensation_available,
                        "max_attempts": policy.max_attempts,
                    },
                    "mcp_failure": failure.to_dict() if failure is not None else None,
                    "mcp_circuit": circuit,
                },
            )
        except MCPAuthorizationError as exc:
            failure = decide_mcp_failure(
                exc.code,
                policy,
                operation_started=False,
            )
            return ToolCallResult(
                call_id=call_id,
                tool_name=tool_name,
                success=False,
                error=exc.code,
                metadata={"mcp_failure": failure.to_dict()},
            )
        except MCPSecretUnavailable:
            error_code = "MCP_SECRET_UNAVAILABLE"
        except TimeoutError:
            # Preserve the existing public error code while exposing the new
            # host-vs-transport distinction in typed metadata.
            error_code = "MCP_TIMEOUT"
            deadline_scope = "host"
        except MCPCircuitOpen as exc:
            error_code = "MCP_CIRCUIT_OPEN"
            if breaker is not None:
                circuit = await breaker.snapshot()
                circuit["retry_after_seconds"] = exc.retry_after
        except MCPError as exc:
            error_code = exc.stable_code
            recovery_evidence = dict(exc.recovery_evidence)
            if error_code == "MCP_TIMEOUT":
                deadline_scope = "transport"
            elif error_code == "MCP_HOST_DEADLINE":
                deadline_scope = "host"
        except asyncio.CancelledError:
            error_code = "MCP_CANCELLED_AFTER_DISPATCH" if operation_started else "MCP_CANCELLED"
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.mcp.runtime.internal_failure", exc
            )
            error_code = "MCP_UPSTREAM_UNAVAILABLE"
        failure = decide_mcp_failure(
            error_code,
            policy,
            operation_started=operation_started,
        )
        if breaker is not None and lease is not None and not circuit_recorded:
            if failure.cause is MCPFailureKind.CANCELLED:
                await breaker.record_neutral(lease)
            elif counts_toward_circuit(failure):
                await breaker.record_failure(lease)
            else:
                await breaker.record_success(lease)
            circuit = await breaker.snapshot()
        if server_id:
            await self._record_runtime_result_best_effort(
                tenant_id=str(getattr(context, "tenant_id", "")),
                server_id=server_id,
                success=False,
                error_code=error_code,
                count_failure=counts_toward_circuit(failure),
            )
        return ToolCallResult(
            call_id=call_id,
            tool_name=tool_name,
            success=False,
            error=error_code,
            metadata={
                "mcp_operation": {
                    "operation_kind": policy.operation_kind.value,
                    "operation_id": policy.operation_id,
                    "idempotency_key_present": bool(policy.idempotency_key),
                    "idempotency_supported": policy.idempotency_supported,
                    "read_back_available": policy.read_back_available,
                    "compensation_available": policy.compensation_available,
                    "max_attempts": policy.max_attempts,
                },
                "mcp_failure": failure.to_dict(),
                "mcp_circuit": circuit,
                "mcp_recovery_evidence": recovery_evidence,
                "mcp_deadline_scope": deadline_scope,
            },
        )


__all__ = [
    "ConfiguredEnvironmentSecretResolver",
    "MCPDiscoveryService",
    "MCPRuntimeService",
    "MCPSecretResolver",
    "MCPSecretUnavailable",
    "MappingSecretResolver",
]
