"""Load MCP server configuration from YAML."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from pathlib import Path

import yaml
from ai_gateway_core.agent_plugins import AgentPluginLoadError, load_agent_plugin
from ai_gateway_core.logging import record_internal_exception

from .client import MCPServerConfig, MCPStaticToolCapability

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/mcp_servers.yaml"
AI_GATEWAY_PLUGIN_EXTENSION = "com.misaya.ai-gateway"
_PLUGIN_MCP_EXTENSION_FIELDS = frozenset(
    {
        "urlEnv",
        "timeoutSeconds",
        "maxConcurrent",
        "responseLimitBytes",
    }
)
_TRUSTED_PLUGIN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?@[0-9A-Za-z.+-]+$")
_BUILTIN_PLUGIN_TOOL_CAPABILITIES = {
    ("ai-docgen@1.0.0", "docgen"): {
        "generate_document": MCPStaticToolCapability.from_config(
            "generate_document",
            {
                "operation_kind": "write",
                "risk_level": "low",
                "requires_confirmation": False,
            },
        )
    }
}
_BUILTIN_PLUGIN_PROCESS_ENV = {
    ("ai-docgen@1.0.0", "docgen"): (
        "DASHSCOPE_CHAT_API_KEY",
        "DASHSCOPE_API_KEY",
        "DOCGEN_LLM_MODEL",
        "DOCGEN_LLM_ENDPOINT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
}
_URL_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_RUNTIME_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")


def _runtime_server_name(declared_name: str) -> str:
    """Map a portable server key to a bounded ToolRegistry-safe name."""

    if _RUNTIME_SERVER_NAME_RE.fullmatch(declared_name):
        return declared_name
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", declared_name).strip("_-")
    base = base[:34].rstrip("_-") or "server"
    digest = hashlib.sha256(declared_name.encode("utf-8")).hexdigest()[:10]
    return f"{base}__{digest}"


def _resolve_env_vars(value: str) -> str:
    """Resolve ${ENV_VAR} placeholders in string values."""

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        return os.environ.get(var, "")

    return re.sub(r"\$\{(\w+)\}", _replace, value)


def load_mcp_config(path: str | None = None) -> list[MCPServerConfig]:
    """Load MCP server configs from YAML file."""
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        logger.debug(f"No MCP config at {config_path}")
        return []

    with open(config_path) as f:
        data = yaml.safe_load(f)

    if not data or not isinstance(data, dict):
        return []

    servers = data.get("mcp_servers", [])
    configs = []
    for srv in servers:
        if not isinstance(srv, dict) or not srv.get("name"):
            continue
        # Static config is platform-managed compatibility only. Credential
        # values must still come from an environment placeholder; a literal
        # would otherwise become committed plaintext configuration.
        raw_key = srv.get("api_key")
        if raw_key and not re.fullmatch(r"\$\{\w+\}", str(raw_key)):
            raise ValueError("MCP api_key must use a ${ENV_VAR} reference")
        api_key = _resolve_env_vars(str(raw_key)) if raw_key else None
        if api_key == "":
            api_key = None

        configs.append(
            MCPServerConfig(
                name=srv["name"],
                url=_resolve_env_vars(str(srv.get("url", ""))),
                api_key=api_key,
                transport="streamable_http",
                timeout=float(srv.get("timeout", 30.0)),
                enabled=srv.get("enabled", True),
                description=srv.get("description", ""),
                allowed_tools=srv.get("allowed_tools"),
                blocked_tools=srv.get("blocked_tools"),
                max_concurrent=int(srv.get("max_concurrent", 10)),
                response_limit_bytes=int(srv.get("response_limit_bytes", 1048576)),
                platform_managed=True,
                allow_localhost=True,
                allow_private_network=True,
                tool_capabilities=srv.get("tool_capabilities", {}),
            )
        )

    logger.info(f"Loaded {len(configs)} MCP server configs from {config_path}")
    return configs


def _plugin_endpoint(
    declared_url: str,
    *,
    override_url: str | None,
) -> tuple[str, str]:
    """Map a portable endpoint URL to MCPClient's origin + endpoint path."""

    declared = urllib.parse.urlsplit(declared_url)
    selected = urllib.parse.urlsplit(override_url) if override_url else declared
    endpoint_path = selected.path or declared.path or "/mcp"
    endpoint_query = selected.query or (declared.query if not selected.path else "")
    if endpoint_query:
        endpoint_path = f"{endpoint_path}?{endpoint_query}"
    origin = urllib.parse.urlunsplit((selected.scheme, selected.netloc, "", "", "")).rstrip("/")
    return origin, endpoint_path


def _trusted_plugin_ids(configured: str | None = None) -> set[str]:
    """Return operator-approved built-in plugin identities from process config."""

    trusted: set[str] = set()
    raw_config = (
        os.getenv("ASSISTANT_TRUSTED_AGENT_PLUGINS", "")
        if configured is None
        else configured
    )
    for raw_id in raw_config.split(","):
        plugin_id = raw_id.strip()
        if not plugin_id:
            continue
        if not _TRUSTED_PLUGIN_ID_RE.fullmatch(plugin_id):
            logger.warning("agent_plugin.trusted_id_invalid")
            continue
        trusted.add(plugin_id)
    return trusted


def _trusted_plugin_roots(configured: str | None = None) -> set[Path]:
    """Resolve operator-owned plugin roots that may launch local code.

    Identity metadata is self-declared, so it is never sufficient to cross the
    stdio execution boundary. Requiring an independently configured canonical
    root prevents a third-party package from impersonating a bundled plugin by
    copying its name and version.
    """

    trusted: set[Path] = set()
    raw_config = (
        os.getenv("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS", "")
        if configured is None
        else configured
    )
    for raw_path in raw_config.split(os.pathsep):
        value = raw_path.strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            logger.warning("agent_plugin.trusted_root_invalid")
            continue
        try:
            root = candidate.resolve(strict=True)
        except OSError:
            logger.warning("agent_plugin.trusted_root_invalid")
            continue
        if not root.is_dir():
            logger.warning("agent_plugin.trusted_root_invalid")
            continue
        trusted.add(root)
    return trusted


def _expand_plugin_placeholders(value: str, *, plugin_root: Path, plugin_data: Path) -> str:
    return value.replace("${PLUGIN_ROOT}", str(plugin_root)).replace(
        "${PLUGIN_DATA}",
        str(plugin_data),
    )


def _plugin_data_dir(plugin_name: str, *, configured_root: str | None = None) -> Path:
    raw_root = (
        os.getenv("ASSISTANT_AGENT_PLUGIN_DATA_ROOT", "/app/data/agent-plugins")
        if configured_root is None
        else configured_root
    )
    base = Path(raw_root).expanduser()
    if not base.is_absolute():
        raise ValueError("ASSISTANT_AGENT_PLUGIN_DATA_ROOT must be absolute")
    return base.resolve(strict=False) / plugin_name


def load_agent_plugin_mcp_config(
    raw_paths: str | None = None,
    *,
    startup_config=None,
) -> list[MCPServerConfig]:
    """Load operator-approved Agent Plugin MCP components.

    A configured plugin path is only a discovery boundary. No HTTP or stdio
    component becomes initializable unless the operator independently approves
    both the self-declared plugin identity and its exact canonical root. After
    that approval, portable URLs remain subject to Agent Plugins URL rules and
    the AI Gateway extension may select an operator-owned URL environment
    variable for container/service discovery.
    """

    configured = raw_paths
    if configured is None:
        configured = (
            str(startup_config.runtime_value("ASSISTANT_AGENT_PLUGIN_PATHS"))
            if startup_config is not None
            else os.getenv("ASSISTANT_AGENT_PLUGIN_PATHS", "")
        )
    configs: list[MCPServerConfig] = []
    seen_names: set[str] = set()
    trusted_plugin_ids = _trusted_plugin_ids(
        str(startup_config.runtime_value("ASSISTANT_TRUSTED_AGENT_PLUGINS"))
        if startup_config is not None
        else None
    )
    trusted_plugin_roots = _trusted_plugin_roots(
        str(startup_config.runtime_value("ASSISTANT_TRUSTED_AGENT_PLUGIN_ROOTS"))
        if startup_config is not None
        else None
    )
    for raw_path in configured.split(os.pathsep):
        if not raw_path.strip():
            continue
        try:
            package = load_agent_plugin(Path(raw_path).expanduser())
        except AgentPluginLoadError as exc:
            record_internal_exception(
                logger, "agent_plugin.mcp_load_rejected", exc, level=logging.WARNING
            )
            continue

        if not package.mcp_servers:
            continue
        plugin_id = f"{package.manifest.name}@{package.manifest.version}"
        trusted_plugin = plugin_id in trusted_plugin_ids and package.root in trusted_plugin_roots
        if not trusted_plugin:
            if plugin_id in trusted_plugin_ids:
                logger.warning("agent_plugin.trusted_root_mismatch plugin=%s", plugin_id)
            else:
                logger.warning("agent_plugin.mcp_untrusted plugin=%s", plugin_id)
            # Installation/discovery never implies executable authority. Keep
            # untrusted package extensions (including urlEnv) inert by rejecting
            # the package before reading any execution-specific settings.
            continue

        raw_extension = package.manifest.extensions.get(
            AI_GATEWAY_PLUGIN_EXTENSION,
            {},
        )
        extension = raw_extension.get("mcp", {}) if isinstance(raw_extension, dict) else {}
        if not isinstance(extension, dict):
            logger.warning("agent_plugin.mcp_extension_invalid plugin=%s", package.manifest.name)
            extension = {}

        for server in package.mcp_servers:
            runtime_name = _runtime_server_name(server.name)
            if runtime_name in seen_names:
                logger.warning("agent_plugin.mcp_name_conflict server=%s", server.name)
                continue
            settings = extension.get(server.name, {})
            if not isinstance(settings, dict) or set(settings) - _PLUGIN_MCP_EXTENSION_FIELDS:
                logger.warning("agent_plugin.mcp_extension_invalid server=%s", server.name)
                settings = {}

            override_url: str | None = None
            url_env = settings.get("urlEnv")
            if url_env is not None and server.transport != "streamable-http":
                logger.warning("agent_plugin.mcp_extension_invalid server=%s", server.name)
                continue
            if url_env is not None:
                if not isinstance(url_env, str) or not _URL_ENV_RE.fullmatch(url_env):
                    logger.warning("agent_plugin.mcp_url_env_invalid server=%s", server.name)
                    continue
                override_url = (
                    startup_config.dynamic_endpoint_value(url_env).strip()
                    if startup_config is not None
                    else os.getenv(url_env, "").strip()
                ) or None

            timeout = settings.get("timeoutSeconds", 30.0)
            max_concurrent = settings.get("maxConcurrent", 10)
            response_limit_bytes = settings.get("responseLimitBytes", 1024 * 1024)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 < float(timeout) <= 600
                or isinstance(max_concurrent, bool)
                or not isinstance(max_concurrent, int)
                or not 1 <= max_concurrent <= 32
                or isinstance(response_limit_bytes, bool)
                or not isinstance(response_limit_bytes, int)
                or not 1024 <= response_limit_bytes <= 8 * 1024 * 1024
            ):
                logger.warning("agent_plugin.mcp_extension_invalid server=%s", server.name)
                continue

            tool_capabilities = (
                _BUILTIN_PLUGIN_TOOL_CAPABILITIES.get((plugin_id, server.name), {})
                if trusted_plugin
                else {}
            )
            if server.transport == "stdio":
                plugin_data = _plugin_data_dir(
                    package.manifest.name,
                    configured_root=(
                        str(
                            startup_config.runtime_value(
                                "ASSISTANT_AGENT_PLUGIN_DATA_ROOT"
                            )
                        )
                        if startup_config is not None
                        else None
                    ),
                )
                command = server.command
                if command.startswith("./"):
                    command = str((package.root / command.removeprefix("./")).resolve(strict=True))
                args = tuple(
                    _expand_plugin_placeholders(
                        value,
                        plugin_root=package.root,
                        plugin_data=plugin_data,
                    )
                    for value in server.args
                )
                process_env = {
                    key: _expand_plugin_placeholders(
                        value,
                        plugin_root=package.root,
                        plugin_data=plugin_data,
                    )
                    for key, value in server.env.items()
                }
                inherited_names = _BUILTIN_PLUGIN_PROCESS_ENV.get(
                    (plugin_id, server.name),
                    (),
                )
                if startup_config is not None:
                    dashscope_key = startup_config.providers["dashscope"].api_key
                    frozen_env: dict[str, str] = {}
                    for name in inherited_names:
                        if name in {"DASHSCOPE_CHAT_API_KEY", "DASHSCOPE_API_KEY"}:
                            if dashscope_key:
                                frozen_env[name] = dashscope_key
                            continue
                        if name in startup_config.runtime:
                            frozen_env[name] = str(startup_config.runtime_value(name))
                    process_env.update(
                        {name: value for name, value in frozen_env.items() if value}
                    )
                    inherited_names = ()
                raw_cwd = server.cwd or "${PLUGIN_ROOT}"
                cwd = _expand_plugin_placeholders(
                    raw_cwd,
                    plugin_root=package.root,
                    plugin_data=plugin_data,
                )
                configs.append(
                    MCPServerConfig(
                        name=runtime_name,
                        transport="stdio",
                        timeout=float(timeout),
                        description=package.manifest.description,
                        max_concurrent=max_concurrent,
                        response_limit_bytes=response_limit_bytes,
                        platform_managed=True,
                        default_tenant_enabled=trusted_plugin,
                        command=command,
                        args=args,
                        process_env=process_env,
                        inherited_env_names=inherited_names,
                        cwd=cwd,
                        plugin_root=str(package.root),
                        plugin_data_dir=str(plugin_data),
                        tool_capabilities=dict(tool_capabilities),
                    )
                )
                seen_names.add(runtime_name)
                continue

            origin, endpoint_path = _plugin_endpoint(
                server.url,
                override_url=override_url,
            )
            configs.append(
                MCPServerConfig(
                    name=runtime_name,
                    url=origin,
                    headers=dict(server.headers),
                    transport="streamable_http",
                    timeout=float(timeout),
                    description=package.manifest.description,
                    max_concurrent=max_concurrent,
                    response_limit_bytes=response_limit_bytes,
                    endpoint_path=endpoint_path,
                    platform_managed=True,
                    default_tenant_enabled=trusted_plugin,
                    allow_localhost=True,
                    allow_private_network=override_url is not None,
                    tool_capabilities=dict(tool_capabilities),
                )
            )
            seen_names.add(runtime_name)
    return configs
