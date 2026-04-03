"""
Tools / Skills module — list available tools and handle approval flows.

Wraps ``/api/v1/skills`` for tool discovery and approval management
within the gateway's execution sandbox.
"""

from __future__ import annotations

from typing import Any

from ai_assistant.models.response import ToolInfo
from ai_assistant.transport.http import HTTPTransport


class ToolModule:
    """High-level tool / skill operations."""

    _SKILLS_BASE = "/api/v1/skills"
    _TOOLS_BASE = "/api/v1/assistant/tools"

    def __init__(self, transport: HTTPTransport) -> None:
        self._transport = transport

    async def list_tools(self, *, limit: int = 100, offset: int = 0) -> list[ToolInfo]:
        """List all registered tools / skills.

        Args:
            limit: Maximum number of tools to return.
            offset: Pagination offset.

        Returns:
            List of ``ToolInfo`` objects.
        """
        params: dict[str, Any] = {"limit": limit}
        if offset > 0:
            params["offset"] = offset
        resp = await self._transport.request("GET", self._TOOLS_BASE)
        data = resp.json()
        tools_raw = data.get("tools", []) if isinstance(data, dict) else data
        return [ToolInfo.from_dict(t) for t in tools_raw]

    async def get(self, name: str) -> ToolInfo:
        """Get details for a single tool / skill by name.

        Raises:
            NotFoundError: If the tool does not exist.
        """
        resp = await self._transport.request("GET", f"{self._SKILLS_BASE}/{name}")
        return ToolInfo.from_dict(resp.json())

    async def enable(self, name: str) -> dict[str, Any]:
        """Enable a tool / skill.

        Args:
            name: Tool name.

        Returns:
            Server confirmation dict.
        """
        resp = await self._transport.request("POST", f"{self._SKILLS_BASE}/{name}/enable")
        return resp.json()

    async def disable(self, name: str) -> dict[str, Any]:
        """Disable a tool / skill.

        Args:
            name: Tool name.

        Returns:
            Server confirmation dict.
        """
        resp = await self._transport.request("POST", f"{self._SKILLS_BASE}/{name}/disable")
        return resp.json()

    async def test(self, name: str, *, input_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a test invocation of a tool / skill.

        Args:
            name: Tool name.
            input_data: Optional test input payload.

        Returns:
            Test execution result dict.
        """
        body: dict[str, Any] = {}
        if input_data is not None:
            body["input"] = input_data
        resp = await self._transport.request("POST", f"{self._SKILLS_BASE}/{name}/test", json=body)
        return resp.json()

    async def approve(self, approval_id: str, approved: bool) -> dict[str, Any]:
        """Respond to a tool approval request.

        When the gateway emits an ``approval_required`` stream event, the
        caller must explicitly approve or deny the action via this method.

        Args:
            approval_id: The approval ID from the ``approval_required`` event.
            approved: ``True`` to approve, ``False`` to deny.

        Returns:
            Server confirmation dict.
        """
        resp = await self._transport.request(
            "POST",
            f"/api/v1/assistant/approvals/{approval_id}",
            json={"approved": approved},
        )
        return resp.json()
