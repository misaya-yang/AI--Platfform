"""Load MCP server configuration from YAML."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from .client import MCPServerConfig

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/mcp_servers.yaml"


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
