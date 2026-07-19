"""
MCP Manager — multi-server orchestration and tool registration.

Connects to all configured MCP servers, discovers their tools, and registers
them into the agent's ToolRegistry with prefix `mcp_{server}:{tool}`.
"""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Any

from ..tools.tool_registry import (
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
    get_tool_registry,
)
from .client import MCPClient, MCPServerConfig, MCPTool

logger = logging.getLogger(__name__)


class MCPManager:
    """Manage multiple MCP server connections and tool registration."""

    def __init__(self, configs: list[MCPServerConfig] | None = None) -> None:
        self._configs = configs or []
        self._clients: dict[str, MCPClient] = {}

    async def initialize_all(self) -> dict[str, int]:
        """Connect to all configured MCP servers and discover tools.

        Returns: {server_name: tool_count} (-1 if failed)
        """
        results: dict[str, int] = {}
        tool_registry = get_tool_registry()

        # Parallel initialization of all servers
        import asyncio

        async def _init_one(config: MCPServerConfig) -> tuple[str, int]:
            if not config.enabled:
                return config.name, 0
            try:
                client = MCPClient(config)
                await client.initialize()
                tools = await client.list_tools()
                self._clients[config.name] = client
                for mcp_tool in tools:
                    self._register_mcp_tool(mcp_tool, client, tool_registry)
                logger.info(f"MCP '{config.name}': {len(tools)} tools registered")
                return config.name, len(tools)
            except Exception as e:
                logger.warning(f"MCP '{config.name}' failed: {e}")
                return config.name, -1

        init_results = await asyncio.gather(
            *[_init_one(c) for c in self._configs],
            return_exceptions=True,
        )
        for r in init_results:
            if isinstance(r, Exception):
                continue
            results[r[0]] = r[1]

        return results

    def _register_mcp_tool(
        self, mcp_tool: MCPTool, client: MCPClient, tool_registry: Any,
    ) -> None:
        """Register an MCP tool as a callable tool in ToolRegistry."""
        # Sanitize tool name for ToolRegistry (no colons allowed in some LLM APIs)
        registry_name = f"mcp_{mcp_tool.server_name}__{mcp_tool.name}"

        # Convert JSON Schema to ToolParameters
        params = self._schema_to_params(mcp_tool.input_schema)

        # Sanitize external tool description (prevent prompt injection and
        # credential-shaped leakage from untrusted MCP servers).
        safe_desc = self._sanitize_external_text(mcp_tool.description or "")
        keywords = self._relevance_keywords(mcp_tool, safe_desc)

        definition = ToolDefinition(
            name=registry_name,
            description=f"[{mcp_tool.server_name}] {safe_desc}",
            parameters=params,
            category=ToolCategory.MCP,
            risk_level=ToolRiskLevel.MEDIUM,
            when_to_use=safe_desc,
            when_not_to_use="When this tenant has not explicitly enabled the MCP server.",
            relevance_keywords=keywords,
            timeout_seconds=int(client.config.timeout),
            is_async=True,
        )
        definition.capability_metadata = self._capability_metadata(
            mcp_tool=mcp_tool,
            registry_name=registry_name,
            safe_description=safe_desc,
            keywords=keywords,
        )

        # Create executor closure
        async def executor(request: Any) -> Any:
            from ..tools.tool_registry import ToolCallResult
            args = getattr(request, "arguments", None) or getattr(request, "tool_args", {}) or {}
            result = await client.call_tool(mcp_tool.upstream_name, args)
            text_parts: list[str] = []
            # File-producing MCP tools return ``type:"resource"`` content with
            # a uri/name/mimeType. We surface these through ToolCallResult.
            # output_files so the standard artifact_persister pipeline emits
            # an ARTIFACT_CREATED event — independent of whether the model
            # correctly transcribes the URL into its text response.
            file_outputs: list[dict[str, Any]] = []
            for c in result.content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "text":
                    text_parts.append(str(c.get("text", "")))
                elif ctype == "resource_link":
                    # MCP "resource_link" content — URL pointer to a file the
                    # tool produced. Flat shape (uri/name/mimeType at top level).
                    uri = c.get("uri") or ""
                    if uri:
                        file_outputs.append({
                            "download_url": uri,
                            "filename": c.get("name") or c.get("title") or "artifact",
                            "mime_type": c.get("mimeType") or c.get("mime_type"),
                            "size_bytes": c.get("size") or c.get("size_bytes"),
                            "source": "mcp",
                            "externally_hosted": True,
                        })
                elif ctype == "resource":
                    # MCP "embedded resource" content — nested resource object.
                    r = c.get("resource") or {}
                    uri = r.get("uri") or ""
                    if uri:
                        file_outputs.append({
                            "download_url": uri,
                            "filename": r.get("name") or r.get("title") or "artifact",
                            "mime_type": r.get("mimeType") or r.get("mime_type"),
                            "size_bytes": r.get("size") or r.get("size_bytes"),
                            "source": "mcp",
                            "externally_hosted": True,
                        })
                elif ctype == "image":
                    # Inline image content — treat as file with data URL.
                    data = c.get("data")
                    mime = c.get("mimeType") or "image/png"
                    if data:
                        file_outputs.append({
                            "download_url": f"data:{mime};base64,{data}",
                            "filename": "image",
                            "mime_type": mime,
                            "source": "mcp_inline",
                            "externally_hosted": True,
                        })
            return ToolCallResult(
                call_id=getattr(request, "call_id", ""),
                tool_name=registry_name,
                success=not result.is_error,
                result="\n".join(text_parts) if text_parts else str(result.content),
                output_files=file_outputs,
                metadata={"mcp_server": mcp_tool.server_name, "mcp_tool": mcp_tool.name},
            )

        tool_registry.register(definition, executor)

    async def refresh_tools(self, server_name: str | None = None) -> dict[str, int]:
        """Re-discover tools from MCP servers. Deregisters stale tools."""
        tool_registry = get_tool_registry()
        targets = [server_name] if server_name else list(self._clients.keys())
        results: dict[str, int] = {}
        for name in targets:
            client = self._clients.get(name)
            if not client:
                results[name] = -1
                continue
            try:
                # Deregister old tools for this server before re-registering
                prefix = f"mcp_{name}__"
                for existing in tool_registry.list_tools():
                    if existing.name.startswith(prefix):
                        tool_registry.unregister(existing.name)

                tools = await client.list_tools()
                for t in tools:
                    self._register_mcp_tool(t, client, tool_registry)
                results[name] = len(tools)
            except Exception as e:
                logger.warning(f"MCP refresh '{name}' failed: {e}")
                results[name] = -1
        return results

    async def shutdown(self) -> None:
        """Close all MCP connections."""
        for client in self._clients.values():
            with suppress(Exception):
                await client.close()
        self._clients.clear()

    @property
    def server_names(self) -> list[str]:
        """Public accessor for configured server names."""
        return [c.name for c in self._configs]

    def get_servers_status(self) -> list[dict]:
        """Return status of all configured MCP servers."""
        status = []
        for config in self._configs:
            client = self._clients.get(config.name)
            status.append({
                "name": config.name,
                "url": config.url,
                "description": config.description,
                "enabled": config.enabled,
                "connected": client is not None and client.is_initialized,
                "tool_count": len(client.tools) if client else 0,
            })
        return status

    def _schema_to_params(self, schema: dict) -> list[ToolParameter]:
        """Convert JSON Schema to ToolParameter list."""
        params = []
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        for name, prop in properties.items():
            params.append(ToolParameter(
                name=name,
                type=prop.get("type", "string"),
                description=self._sanitize_external_text(prop.get("description", "")),
                required=name in required,
            ))
        return params

    def _sanitize_external_text(self, text: str, max_len: int = 500) -> str:
        """Bound and redact untrusted MCP-provided display text."""
        sanitized = re.sub(r"[\x00-\x1f\x7f]", " ", text)
        sanitized = re.sub(
            r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+",
            r"\1[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            sanitized,
        )
        sanitized = re.sub(
            r"(?i)(ignore\s+(all\s+)?previous|system\s+prompt|developer\s+message|jailbreak)",
            "[untrusted-instruction]",
            sanitized,
        )
        sanitized = " ".join(sanitized.split())
        return sanitized[:max_len]

    def _relevance_keywords(self, mcp_tool: MCPTool, safe_description: str) -> list[str]:
        """Build bounded MCP selection keywords from catalog metadata only."""
        raw_values = [mcp_tool.server_name, mcp_tool.name, safe_description]
        seen: set[str] = set()
        keywords: list[str] = []
        for value in raw_values:
            for token in re.findall(r"[a-zA-Z0-9_:-]{3,}", str(value).lower()):
                normalized = token.strip("_:-")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    keywords.append(normalized)
        return keywords[:40]

    def _capability_metadata(
        self,
        mcp_tool: MCPTool,
        registry_name: str,
        safe_description: str,
        keywords: list[str],
    ) -> dict[str, Any]:
        """Expose MCP catalog facts without loading remote schema/resource data."""
        return {
            "kind": "mcp",
            "mcp_server": mcp_tool.server_name,
            "mcp_tool": mcp_tool.name,
            "tool_name": registry_name,
            "summary": safe_description,
            "setup_state": "ready",
            "policy_scope": "tenant",
            "external_service": True,
            "trigger_examples": keywords[:8],
            "progressive_disclosure": {
                "level0": [
                    "name",
                    "description",
                    "category",
                    "risk_level",
                    "setup_state",
                    "trigger_examples",
                    "mcp_server",
                    "mcp_tool",
                    "policy_scope",
                ],
                "level1_available": True,
                "level2_loaded": False,
                "schema_loaded_on_demand": True,
            },
        }
