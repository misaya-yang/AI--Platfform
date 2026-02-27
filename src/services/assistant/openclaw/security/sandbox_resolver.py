"""Sandbox policy resolver for tool/skill execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class SandboxDecision:
    """Resolved sandbox decision for execution."""

    allowed: bool
    sandbox: str
    reason: str
    requires_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SandboxResolver:
    """Apply hard sandbox boundaries based on profile/tool risk."""

    HIGH_RISK_TOOLS = {
        "execute_python_code",
        "system.run",
        "node.invoke",
    }

    MEDIUM_RISK_TOOLS = {
        "search_web",
        "web_search",
        "generate_image",
    }

    def resolve(
        self,
        *,
        tool_name: str,
        execution_profile: str,
        os_agent_enabled: bool,
    ) -> SandboxDecision:
        profile = (execution_profile or "safe").lower()

        if tool_name in self.HIGH_RISK_TOOLS:
            if not os_agent_enabled and tool_name in {"system.run", "node.invoke"}:
                return SandboxDecision(
                    allowed=False,
                    sandbox="deny",
                    reason="OS agent execution disabled",
                )
            if profile == "safe":
                return SandboxDecision(
                    allowed=True,
                    sandbox="docker_restricted",
                    reason="High-risk tool under safe profile",
                    requires_approval=True,
                )
            return SandboxDecision(
                allowed=True,
                sandbox="docker_restricted",
                reason="High-risk tool allowed with restricted sandbox",
                requires_approval=True,
            )

        if tool_name in self.MEDIUM_RISK_TOOLS and profile == "safe":
            return SandboxDecision(
                allowed=True,
                sandbox="network_restricted",
                reason="Medium-risk tool under safe profile",
                requires_approval=False,
            )

        return SandboxDecision(
            allowed=True,
            sandbox="default",
            reason="Default sandbox policy",
            requires_approval=False,
        )
