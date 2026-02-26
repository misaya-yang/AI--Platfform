"""Assistant policy engine for routing, isolation, and approval decisions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ....core.observability.logging import get_logger

logger = get_logger(__name__)


TIER_ORDER = {
    "anonymous": 0,
    "normal": 1,
    "premium": 2,
    "enterprise": 3,
    "admin": 4,
}


@dataclass
class ToolPolicyDecision:
    """Decision for a single tool invocation."""

    allowed: bool
    requires_approval: bool
    reason: str | None = None
    policy_profile: str = "safe"


class AssistantPolicyEngine:
    """Evaluate tool execution policy with safe defaults."""

    HIGH_RISK_TOOLS = {
        "system_run_lite",
        "browser_action_lite",
    }

    MEDIUM_RISK_TOOLS = {
        "execute_python_code",
    }

    def __init__(
        self,
        os_agent_default_enabled: bool = False,
        default_execution_profile: str = "safe",
        default_memory_mode: str = "auto",
    ) -> None:
        self.os_agent_default_enabled = os_agent_default_enabled
        self.default_execution_profile = default_execution_profile
        self.default_memory_mode = default_memory_mode

    @classmethod
    def from_env(cls) -> AssistantPolicyEngine:
        os_agent_enabled = os.getenv("ASSISTANT_OS_AGENT_LITE", "false").lower() == "true"
        default_profile = os.getenv("ASSISTANT_DEFAULT_EXECUTION_PROFILE", "safe")
        default_memory = os.getenv("ASSISTANT_DEFAULT_MEMORY_MODE", "auto")
        return cls(
            os_agent_default_enabled=os_agent_enabled,
            default_execution_profile=default_profile,
            default_memory_mode=default_memory,
        )

    def evaluate_tool(
        self,
        tool_name: str,
        context: Any,
        execution_profile: str = "safe",
        os_agent_enabled: bool = False,
    ) -> ToolPolicyDecision:
        """Return allow/approval decision for a tool call."""
        _ = context
        profile = execution_profile or self.default_execution_profile

        if tool_name in self.HIGH_RISK_TOOLS:
            if not (os_agent_enabled or self.os_agent_default_enabled):
                return ToolPolicyDecision(
                    allowed=False,
                    requires_approval=False,
                    reason="OS-Agent Lite is disabled by policy",
                    policy_profile=profile,
                )
            return ToolPolicyDecision(
                allowed=True,
                requires_approval=True,
                reason="High-risk tool requires explicit approval",
                policy_profile=profile,
            )

        if tool_name in self.MEDIUM_RISK_TOOLS and profile == "safe":
            return ToolPolicyDecision(
                allowed=True,
                requires_approval=True,
                reason="Safe profile requires approval for medium-risk tools",
                policy_profile=profile,
            )

        return ToolPolicyDecision(
            allowed=True,
            requires_approval=False,
            reason=None,
            policy_profile=profile,
        )

    def user_can_use_os_agent(self, user: Any) -> bool:
        """Strict default: only enterprise/admin roles or explicit feature enable."""
        if self.os_agent_default_enabled:
            return True
        if user is None:
            return False
        tier = getattr(user, "tier", "anonymous")
        roles = set(getattr(user, "roles", []) or [])
        return TIER_ORDER.get(tier, 0) >= TIER_ORDER["enterprise"] or "admin" in roles

    def get_public_policies(self) -> dict[str, Any]:
        """Return policies safe to expose via API."""
        return {
            "default_execution_profile": self.default_execution_profile,
            "default_memory_mode": self.default_memory_mode,
            "os_agent_default_enabled": self.os_agent_default_enabled,
            "high_risk_tools": sorted(self.HIGH_RISK_TOOLS),
            "medium_risk_tools": sorted(self.MEDIUM_RISK_TOOLS),
        }
