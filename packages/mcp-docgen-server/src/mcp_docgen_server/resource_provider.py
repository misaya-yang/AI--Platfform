"""Catalog data exposed as MCP Resources (not tools).

Three URI namespaces — kept deliberately dumb so tests can exercise them
without spinning up an MCP session:

  docgen://design-systems            list of {name, description}
  docgen://templates/{tenant_id}     list of templates for a tenant
  docgen://skills/{doc_type}         body of the matching SKILL.md

Progressive disclosure lives here: the agent calls ``list_resources``
to see what's available, then ``read_resource(uri)`` on demand. Avoids
burning tokens on skills the agent never uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DESIGN_SYSTEMS_URI = "docgen://design-systems"
TEMPLATES_URI_PREFIX = "docgen://templates/"
SKILLS_URI_PREFIX = "docgen://skills/"


@dataclass(frozen=True)
class ResourceDescriptor:
    """Minimal descriptor — lifted into ``mcp.types.Resource`` at the edge."""

    uri: str
    name: str
    description: str
    mime_type: str


def _design_system_descriptors() -> list[ResourceDescriptor]:
    """Static descriptor for the design-systems catalog endpoint.

    Returns one descriptor — the catalog itself. Individual systems are
    read as a JSON array from the catalog URI rather than exposed as
    one-resource-per-system (avoids N resources for N <= 10 systems).
    """
    return [
        ResourceDescriptor(
            uri=DESIGN_SYSTEMS_URI,
            name="docgen.design_systems",
            description=(
                "JSON array of available design systems "
                "(name + description). Pass a name to the "
                "generate_document tool via `design_system`."
            ),
            mime_type="application/json",
        )
    ]


def _template_descriptor(tenant_id: str) -> ResourceDescriptor:
    return ResourceDescriptor(
        uri=f"{TEMPLATES_URI_PREFIX}{tenant_id}",
        name=f"docgen.templates[{tenant_id}]",
        description=(
            f"JSON array of brand templates available to tenant "
            f"{tenant_id!r}. Pass a template name via `template_name`."
        ),
        mime_type="application/json",
    )


def _skill_descriptor(doc_type: str) -> ResourceDescriptor:
    return ResourceDescriptor(
        uri=f"{SKILLS_URI_PREFIX}{doc_type}",
        name=f"docgen.skills.{doc_type}",
        description=(
            f"SKILL.md body for doc_type={doc_type!r}. "
            "Read on-demand; do not preload into context."
        ),
        mime_type="text/markdown",
    )


def list_static_resources(
    *,
    tenant_ids: Optional[list[str]] = None,
    doc_types: Optional[list[str]] = None,
) -> list[ResourceDescriptor]:
    """Build the static resource list for ``list_resources``.

    ``tenant_ids`` and ``doc_types`` are hints — the server can still
    resolve ``docgen://templates/<anything>`` and ``docgen://skills/<anything>``
    URIs that weren't pre-listed (MCP clients commonly ask for resources
    they discovered through other channels).
    """
    out = list(_design_system_descriptors())
    for tid in tenant_ids or []:
        out.append(_template_descriptor(tid))
    for dt in doc_types or ("docx", "pptx", "xlsx", "pdf"):
        out.append(_skill_descriptor(dt))
    return out


# ---------------------------------------------------------------------------
# URI → bytes
#
# Each reader takes the already-parsed URI tail and the docgen wiring it
# needs, and returns ``(mime_type, text_payload)``. Kept as free
# functions — no MCP types leak in, so tests are trivial.


def read_design_systems(service: Any) -> tuple[str, str]:
    """Return the design-systems catalog as JSON.

    ``service`` is a DocgenService — we only call ``.design_system_catalog()``
    on it (if it exists), otherwise fall back to the module-level helper.
    """
    # Prefer a method on the service (more testable / mockable);
    # fall back to the module import so we still work with vanilla
    # DocgenService instances.
    catalog: list[dict]
    available_systems_fn = getattr(service, "design_system_catalog", None)
    if callable(available_systems_fn):
        catalog = list(available_systems_fn())
    else:
        from docgen.design_system import available_systems, get_design_system

        catalog = []
        for name in available_systems():
            try:
                ds = get_design_system(name)
                catalog.append({"name": name, "description": ds.description})
            except Exception:  # pragma: no cover — defensive
                catalog.append({"name": name, "description": ""})
    return "application/json", json.dumps(catalog, ensure_ascii=False, indent=2)


def read_templates(service: Any, tenant_id: str) -> tuple[str, str]:
    """Return the templates list for ``tenant_id`` as JSON.

    Touches the service's TemplateRegistry via the protected attribute
    ``_templates`` if the service did not expose a public accessor. We
    keep this loose so docgen internals can evolve.
    """
    templates_attr = getattr(service, "templates", None) or getattr(service, "_templates", None)
    if templates_attr is None:
        raise RuntimeError("DocgenService does not expose a template registry")
    items = templates_attr.list(tenant_id=tenant_id)
    payload = [
        {
            "name": t.name,
            "description": t.description,
            "palette_name": getattr(t, "palette_name", None),
            "font_pair_name": getattr(t, "font_pair_name", None),
        }
        for t in items
    ]
    return "application/json", json.dumps(payload, ensure_ascii=False, indent=2)


def read_skill(service: Any, doc_type: str) -> tuple[str, str]:
    """Return the SKILL.md body for ``doc_type`` as markdown.

    Uses the service's ``expand_skill`` helper — that returns the
    compiled SKILL body with all the progressive-disclosure logic
    already applied.
    """
    expand = getattr(service, "expand_skill", None)
    if not callable(expand):
        raise RuntimeError("DocgenService does not expose expand_skill()")
    body = expand(doc_type) or ""
    if not body.strip():
        # Still valid — the agent gets an empty body and an explicit
        # note so it doesn't retry the same URI in a tight loop.
        body = f"# No skill available for doc_type={doc_type!r}\n"
    return "text/markdown", body


# ---------------------------------------------------------------------------
# URI parsing


def parse_uri(uri: str) -> tuple[str, Optional[str]]:
    """Split a ``docgen://`` URI into ``(kind, tail)``.

    kind ∈ {"design-systems", "templates", "skills"}
    tail is the segment after the kind (tenant_id or doc_type) — ``None``
    for the singleton design-systems resource.

    Raises ``ValueError`` for anything else so the caller returns a
    proper MCP error rather than silently serving wrong bytes.
    """
    if not uri.startswith("docgen://"):
        raise ValueError(f"not a docgen URI: {uri!r}")
    rest = uri[len("docgen://") :]
    if rest == "design-systems":
        return "design-systems", None
    if rest.startswith("templates/"):
        tail = rest[len("templates/") :].strip("/")
        if not tail:
            raise ValueError(f"templates URI missing tenant_id: {uri!r}")
        return "templates", tail
    if rest.startswith("skills/"):
        tail = rest[len("skills/") :].strip("/")
        if not tail:
            raise ValueError(f"skills URI missing doc_type: {uri!r}")
        return "skills", tail
    raise ValueError(f"unknown docgen URI: {uri!r}")
