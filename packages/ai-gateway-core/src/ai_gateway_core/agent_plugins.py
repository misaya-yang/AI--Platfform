"""Agent Plugins 1.0.0 directory loader.

The portable v1 format permits incremental client adoption.  AI Gateway is a
skills-only client for now: plugin Skills are loaded as instruction-only
artifacts, while ``mcp.json`` is reported but never started.  This keeps plugin
installation from becoming an implicit code-execution or authorization path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from .skills.models import SkillManifest, SkillSource

PLUGIN_SCHEMA_V1: Final = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
_PLUGIN_FIELDS: Final = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
_PLUGIN_NAME_RE: Final = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_SKILL_NAME_RE: Final = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_MAX_PLUGIN_MANIFEST_BYTES: Final = 64 * 1024
_MAX_SKILL_BYTES: Final = 50_000


class AgentPluginLoadError(ValueError):
    """Stable, content-free plugin rejection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AgentPluginDiagnostic:
    code: str
    component: str
    severity: str = "warning"

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "component": self.component,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class AgentPluginManifest:
    name: str
    version: str = ""
    description: str = ""
    author: dict[str, str] = field(default_factory=dict)
    homepage: str = ""
    repository: str = ""
    license: str = ""
    keywords: tuple[str, ...] = ()
    extensions: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedAgentPlugin:
    root: Path
    manifest: AgentPluginManifest
    skills: tuple[SkillManifest, ...]
    diagnostics: tuple[AgentPluginDiagnostic, ...]
    mcp_present: bool = False


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentPluginLoadError("AGENT_PLUGIN_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _read_contained_file(
    path: Path,
    *,
    root: Path,
    limit: int,
    invalid_code: str,
) -> str:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AgentPluginLoadError(invalid_code) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise AgentPluginLoadError(invalid_code)
    try:
        if resolved.stat().st_size > limit:
            raise AgentPluginLoadError(f"{invalid_code}_TOO_LARGE")
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentPluginLoadError(invalid_code) from exc
    except UnicodeError as exc:
        raise AgentPluginLoadError(f"{invalid_code}_ENCODING") from exc


def _validate_manifest(raw: Any) -> tuple[AgentPluginManifest, list[AgentPluginDiagnostic]]:
    if not isinstance(raw, dict):
        raise AgentPluginLoadError("AGENT_PLUGIN_MANIFEST_NOT_OBJECT")

    diagnostics = [
        AgentPluginDiagnostic(
            code="AGENT_PLUGIN_MANIFEST_UNKNOWN_FIELD",
            component=str(field_name),
        )
        for field_name in sorted(set(raw) - _PLUGIN_FIELDS)
    ]
    schema = raw.get("$schema")
    if schema != PLUGIN_SCHEMA_V1:
        raise AgentPluginLoadError("AGENT_PLUGIN_SCHEMA_UNSUPPORTED")

    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not _PLUGIN_NAME_RE.fullmatch(name):
        raise AgentPluginLoadError("AGENT_PLUGIN_NAME_INVALID")

    string_fields: dict[str, str] = {}
    for field_name in (
        "version",
        "description",
        "homepage",
        "repository",
        "license",
    ):
        value = raw.get(field_name, "")
        if not isinstance(value, str):
            raise AgentPluginLoadError("AGENT_PLUGIN_MANIFEST_FIELD_INVALID")
        string_fields[field_name] = value

    author = raw.get("author", {})
    if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
        raise AgentPluginLoadError("AGENT_PLUGIN_AUTHOR_INVALID")
    if any(not isinstance(value, str) for value in author.values()):
        raise AgentPluginLoadError("AGENT_PLUGIN_AUTHOR_INVALID")

    keywords = raw.get("keywords", [])
    if not isinstance(keywords, list) or any(not isinstance(value, str) for value in keywords):
        raise AgentPluginLoadError("AGENT_PLUGIN_KEYWORDS_INVALID")

    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict):
        diagnostics.append(
            AgentPluginDiagnostic(
                code="AGENT_PLUGIN_EXTENSIONS_IGNORED",
                component="extensions",
            )
        )
        extensions = {}
    elif any(not isinstance(value, dict) for value in extensions.values()):
        raise AgentPluginLoadError("AGENT_PLUGIN_EXTENSIONS_INVALID")

    return (
        AgentPluginManifest(
            name=name,
            author={str(key): str(value) for key, value in author.items()},
            keywords=tuple(keywords),
            extensions={str(key): dict(value) for key, value in extensions.items()},
            **string_fields,
        ),
        diagnostics,
    )


def _parse_agent_skill(
    content: str,
    *,
    directory_name: str,
    plugin: AgentPluginManifest,
) -> SkillManifest:
    if "\x00" in content:
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_INVALID")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", normalized, re.DOTALL)
    if not match:
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_FRONTMATTER_REQUIRED")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_FRONTMATTER_INVALID") from exc
    if not isinstance(metadata, dict):
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_FRONTMATTER_INVALID")

    name = metadata.get("name")
    description = metadata.get("description")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or not _SKILL_NAME_RE.fullmatch(name)
        or name != directory_name
    ):
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_NAME_INVALID")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_DESCRIPTION_INVALID")

    for optional_field in ("license", "compatibility", "allowed-tools"):
        value = metadata.get(optional_field)
        if value is not None and not isinstance(value, str):
            raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_FRONTMATTER_INVALID")
    compatibility = metadata.get("compatibility")
    if isinstance(compatibility, str) and not 1 <= len(compatibility) <= 500:
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_COMPATIBILITY_INVALID")
    custom_metadata = metadata.get("metadata", {})
    if not isinstance(custom_metadata, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in custom_metadata.items()
    ):
        raise AgentPluginLoadError("AGENT_PLUGIN_SKILL_METADATA_INVALID")

    # ``allowed-tools`` is descriptive only. A third-party package cannot
    # grant itself tools or permissions; the existing runtime policy remains
    # authoritative.
    return SkillManifest(
        name=name,
        title=name,
        description=description,
        entrypoint=f"md://agent-plugin/{plugin.name}/{name}",
        summary=description[:180],
        version=plugin.version or custom_metadata.get("version", "1.0.0"),
        tags=list(plugin.keywords),
        permissions=[],
        enabled=True,
        artifact_type="agent_plugin_instruction",
        instructions=match.group(2).strip(),
        config={
            "agent_plugin": plugin.name,
            "declared_allowed_tools": metadata.get("allowed-tools", ""),
        },
        source=SkillSource.MARKETPLACE,
        max_context_tokens=2000,
        author=plugin.author.get("name", ""),
        generated=False,
        lifecycle_status="active",
    )


def load_agent_plugin(path: str | Path) -> LoadedAgentPlugin:
    """Load one portable Agent Plugin directory without executing package code."""

    try:
        root = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise AgentPluginLoadError("AGENT_PLUGIN_ROOT_INVALID") from exc
    if not root.is_dir():
        raise AgentPluginLoadError("AGENT_PLUGIN_ROOT_INVALID")

    manifest_text = _read_contained_file(
        root / "plugin.json",
        root=root,
        limit=_MAX_PLUGIN_MANIFEST_BYTES,
        invalid_code="AGENT_PLUGIN_MANIFEST_INVALID",
    )
    try:
        raw_manifest = json.loads(manifest_text, object_pairs_hook=_reject_duplicate_keys)
    except AgentPluginLoadError:
        raise
    except json.JSONDecodeError as exc:
        raise AgentPluginLoadError("AGENT_PLUGIN_MANIFEST_JSON_INVALID") from exc
    manifest, diagnostics = _validate_manifest(raw_manifest)

    skills: list[SkillManifest] = []
    skills_path = root / "skills"
    if skills_path.exists():
        try:
            resolved_skills = skills_path.resolve(strict=True)
        except OSError:
            resolved_skills = skills_path
        if not resolved_skills.is_relative_to(root) or not resolved_skills.is_dir():
            diagnostics.append(
                AgentPluginDiagnostic(
                    code="AGENT_PLUGIN_SKILLS_COMPONENT_INVALID",
                    component="skills",
                )
            )
        else:
            for child in sorted(resolved_skills.iterdir(), key=lambda item: item.name):
                try:
                    resolved_child = child.resolve(strict=True)
                except OSError:
                    continue
                if not resolved_child.is_relative_to(root) or not resolved_child.is_dir():
                    diagnostics.append(
                        AgentPluginDiagnostic(
                            code="AGENT_PLUGIN_SKILL_PATH_INVALID",
                            component=child.name,
                        )
                    )
                    continue
                skill_path = resolved_child / "SKILL.md"
                if not skill_path.exists():
                    continue
                try:
                    skill_text = _read_contained_file(
                        skill_path,
                        root=root,
                        limit=_MAX_SKILL_BYTES,
                        invalid_code="AGENT_PLUGIN_SKILL_INVALID",
                    )
                    skills.append(
                        _parse_agent_skill(
                            skill_text,
                            directory_name=child.name,
                            plugin=manifest,
                        )
                    )
                except AgentPluginLoadError as exc:
                    diagnostics.append(AgentPluginDiagnostic(code=exc.code, component=child.name))

    mcp_present = (root / "mcp.json").exists()
    if mcp_present:
        diagnostics.append(
            AgentPluginDiagnostic(
                code="AGENT_PLUGIN_MCP_UNSUPPORTED",
                component="mcp.json",
            )
        )

    return LoadedAgentPlugin(
        root=root,
        manifest=manifest,
        skills=tuple(skills),
        diagnostics=tuple(diagnostics),
        mcp_present=mcp_present,
    )


__all__ = [
    "AgentPluginDiagnostic",
    "AgentPluginLoadError",
    "AgentPluginManifest",
    "LoadedAgentPlugin",
    "PLUGIN_SCHEMA_V1",
    "load_agent_plugin",
]
