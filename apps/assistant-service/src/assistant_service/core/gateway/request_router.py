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
    runtime_mode: str = "compat"
    queue_mode: str = "collect"
    context_detail: bool = False
    skills_enabled: bool | None = None
    memory_profile: str | None = None


class AssistantRequestRouter:
    """Normalize incoming request configuration into a policy route."""

    ALLOWED_EXECUTION_PROFILES = {"safe", "balanced", "power"}
    ALLOWED_MEMORY_MODES = {"auto", "strict", "off"}
    ALLOWED_RUNTIME_MODES = {"off", "compat", "full"}
    ALLOWED_QUEUE_MODES = {"collect", "followup", "steer", "interrupt"}
    ALLOWED_MEMORY_PROFILES = {"off", "basic", "hybrid"}

    def __init__(self, policy_engine: AssistantPolicyEngine | None = None) -> None:
        self.policy_engine = policy_engine or AssistantPolicyEngine.from_env()

    def route(self, config: Any, user: Any | None = None) -> RoutedAssistantRequest:
        profile = (
            getattr(config, "execution_profile", None)
            or self.policy_engine.default_execution_profile
        )
        if profile not in self.ALLOWED_EXECUTION_PROFILES:
            profile = self.policy_engine.default_execution_profile

        memory_mode = (
            str(getattr(config, "memory_mode", None) or self.policy_engine.default_memory_mode)
            .strip()
            .lower()
        )
        if memory_mode not in self.ALLOWED_MEMORY_MODES:
            memory_mode = self.policy_engine.default_memory_mode

        runtime_mode = str(getattr(config, "runtime_mode", "compat") or "compat").strip().lower()
        if runtime_mode not in self.ALLOWED_RUNTIME_MODES:
            runtime_mode = "compat"

        queue_mode = str(getattr(config, "queue_mode", "collect") or "collect").strip().lower()
        if queue_mode not in self.ALLOWED_QUEUE_MODES:
            queue_mode = "collect"

        memory_profile_raw = getattr(config, "memory_profile", None)
        memory_profile = (
            str(memory_profile_raw).strip().lower() if memory_profile_raw is not None else None
        )
        if memory_profile not in self.ALLOWED_MEMORY_PROFILES:
            memory_profile = None
        if memory_mode == "off":
            memory_profile = "off"

        raw_skills_enabled = getattr(config, "skills_enabled", None)
        if raw_skills_enabled is None:
            skills_enabled: bool | None = None
        elif isinstance(raw_skills_enabled, str):
            lowered = raw_skills_enabled.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                skills_enabled = True
            elif lowered in {"false", "0", "no", "off"}:
                skills_enabled = False
            else:
                skills_enabled = None
        else:
            skills_enabled = bool(raw_skills_enabled)

        requested_os_agent = bool(getattr(config, "os_agent_enabled", False))
        os_agent_enabled = requested_os_agent and self.policy_engine.user_can_use_os_agent(user)

        return RoutedAssistantRequest(
            execution_profile=profile,
            memory_mode=memory_mode,
            os_agent_enabled=os_agent_enabled,
            policy_profile=profile,
            runtime_mode=runtime_mode,
            queue_mode=queue_mode,
            context_detail=bool(getattr(config, "context_detail", False)),
            skills_enabled=skills_enabled,
            memory_profile=memory_profile,
        )
