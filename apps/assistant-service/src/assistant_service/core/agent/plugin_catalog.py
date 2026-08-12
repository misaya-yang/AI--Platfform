"""Process-scoped catalog for inert Agent Plugin agent definitions.

Agent definitions are an AI Gateway client extension, not a portable Agent
Plugins v1 component and not runtime authority. This catalog only discovers
validated package data. It never initializes MCP, imports plugin code, or
executes package scripts.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ai_gateway_core.agent_plugins import (
    AgentPluginAgent,
    AgentPluginDiagnostic,
    AgentPluginLoadError,
    LoadedAgentPlugin,
    load_agent_plugin,
)

logger = logging.getLogger(__name__)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentPluginCatalogEntry:
    """One isolated plugin discovery outcome."""

    status: str
    plugin: str = ""
    agent_ids: tuple[str, ...] = ()
    diagnostics: tuple[AgentPluginDiagnostic, ...] = ()
    code: str = ""

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": self.status,
            "plugin": self.plugin,
            "agents": list(self.agent_ids),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
        if self.code:
            result["code"] = self.code
        return result


@dataclass(frozen=True)
class AgentPluginCatalog:
    """Validated, non-executable plugin agent definitions for one process."""

    enabled: bool = False
    agents: tuple[AgentPluginAgent, ...] = ()
    entries: tuple[AgentPluginCatalogEntry, ...] = ()

    @classmethod
    def from_env(cls) -> AgentPluginCatalog:
        """Load configured definitions only when subagents are explicitly on."""

        return cls.load(
            os.getenv("ASSISTANT_AGENT_PLUGIN_PATHS", ""),
            enabled=_env_truthy("ASSISTANT_SUBAGENTS_ENABLED"),
        )

    @classmethod
    def load(cls, raw_paths: str, *, enabled: bool) -> AgentPluginCatalog:
        """Load paths independently, preserving valid siblings on failures."""

        if not enabled:
            return cls(enabled=False)
        if not raw_paths.strip():
            return cls(enabled=True)

        outcomes: list[AgentPluginCatalogEntry | LoadedAgentPlugin] = []
        seen_paths: set[Path] = set()

        for raw_path in raw_paths.split(os.pathsep):
            if not raw_path.strip():
                continue
            try:
                resolved = Path(raw_path).expanduser().resolve(strict=True)
            except OSError:
                outcomes.append(
                    AgentPluginCatalogEntry(
                        status="rejected",
                        code="AGENT_PLUGIN_ROOT_INVALID",
                    )
                )
                logger.warning("agent_plugin_catalog.rejected code=AGENT_PLUGIN_ROOT_INVALID")
                continue
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            try:
                package = load_agent_plugin(resolved)
            except AgentPluginLoadError as exc:
                outcomes.append(AgentPluginCatalogEntry(status="rejected", code=exc.code))
                logger.warning("agent_plugin_catalog.rejected code=%s", exc.code)
                continue
            except Exception as exc:
                outcomes.append(
                    AgentPluginCatalogEntry(
                        status="rejected",
                        code="AGENT_PLUGIN_LOAD_FAILED",
                    )
                )
                logger.warning(
                    "agent_plugin_catalog.rejected code=AGENT_PLUGIN_LOAD_FAILED exception_type=%s",
                    type(exc).__name__,
                )
                continue
            outcomes.append(package)

        id_counts = Counter(
            definition.qualified_id
            for outcome in outcomes
            if isinstance(outcome, LoadedAgentPlugin)
            for definition in outcome.agents
        )
        conflicted_ids = {agent_id for agent_id, count in id_counts.items() if count > 1}
        agents: list[AgentPluginAgent] = []
        entries: list[AgentPluginCatalogEntry] = []
        for outcome in outcomes:
            if isinstance(outcome, AgentPluginCatalogEntry):
                entries.append(outcome)
                continue

            package = outcome
            diagnostics = list(package.diagnostics)
            registered: list[str] = []
            package_conflicted = False
            for definition in package.agents:
                if definition.qualified_id in conflicted_ids:
                    package_conflicted = True
                    diagnostics.append(
                        AgentPluginDiagnostic(
                            code="AGENT_PLUGIN_AGENT_ID_CONFLICT",
                            component=definition.qualified_id,
                        )
                    )
                    continue
                agents.append(definition)
                registered.append(definition.qualified_id)

            entries.append(
                AgentPluginCatalogEntry(
                    status="conflicted" if package_conflicted else "loaded",
                    plugin=package.manifest.name,
                    agent_ids=tuple(registered),
                    diagnostics=tuple(diagnostics),
                )
            )

        logger.info(
            "agent_plugin_catalog.loaded plugins=%s agents=%s",
            sum(entry.status in {"loaded", "conflicted"} for entry in entries),
            len(agents),
        )
        return cls(enabled=True, agents=tuple(agents), entries=tuple(entries))

    @property
    def status(self) -> tuple[dict[str, object], ...]:
        return tuple(entry.to_dict() for entry in self.entries)


__all__ = [
    "AgentPluginCatalog",
    "AgentPluginCatalogEntry",
]
