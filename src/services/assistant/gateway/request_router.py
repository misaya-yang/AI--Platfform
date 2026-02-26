"""Request router for Assistant gateway policy profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policy_engine import AssistantPolicyEngine


@dataclass
class RoutedAssistantRequest:
    """Routing decisions for a single assistant request."""

    execution_profile: str
    memory_mode: str
    os_agent_enabled: bool
    policy_profile: str


class AssistantRequestRouter:
    """Normalize incoming request configuration into a policy route."""

    ALLOWED_EXECUTION_PROFILES = {"safe", "balanced", "power"}
    ALLOWED_MEMORY_MODES = {"auto", "strict", "off"}

    def __init__(self, policy_engine: AssistantPolicyEngine | None = None) -> None:
        self.policy_engine = policy_engine or AssistantPolicyEngine.from_env()

    def route(self, config: Any, user: Any | None = None) -> RoutedAssistantRequest:
        profile = getattr(config, "execution_profile", None) or self.policy_engine.default_execution_profile
        if profile not in self.ALLOWED_EXECUTION_PROFILES:
            profile = self.policy_engine.default_execution_profile

        memory_mode = getattr(config, "memory_mode", None) or self.policy_engine.default_memory_mode
        if memory_mode not in self.ALLOWED_MEMORY_MODES:
            memory_mode = self.policy_engine.default_memory_mode

        requested_os_agent = bool(getattr(config, "os_agent_enabled", False))
        os_agent_enabled = requested_os_agent and self.policy_engine.user_can_use_os_agent(user)

        return RoutedAssistantRequest(
            execution_profile=profile,
            memory_mode=memory_mode,
            os_agent_enabled=os_agent_enabled,
            policy_profile=profile,
        )
