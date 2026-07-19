"""Brand templates — 3 built-ins (default, minimal, enterprise-dark).

A Template ships a :class:`Theme` + default :class:`FontSpec`s + an optional
cover/header treatment. Callers either pick a template by name or hand a
full ``Template`` object. Custom tenant templates are registered at
runtime via :meth:`TemplateRegistry.register`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .ir import Theme
from .style_guide import FONT_PAIRS, PALETTES, theme_from


@dataclass(frozen=True)
class Template:
    """A brand template.

    ``palette_name`` and ``font_pair_name`` reference style_guide entries.
    ``header_text`` is what shows up in the DOCX header / PPT master
    footer / PDF running header.
    """

    name: str
    description: str
    palette_name: str
    font_pair_name: str
    accent_style: Literal["none", "underline", "left_bar"] = "none"
    header_text: str | None = None
    cover_treatment: Literal["minimal", "full", "none"] = "minimal"

    def theme(self) -> Theme:
        return theme_from(self.palette_name, self.font_pair_name, accent_style=self.accent_style)


_BUILTINS: dict[str, Template] = {
    "default": Template(
        name="default",
        description="Safe corporate default — navy + Helvetica + no accents.",
        palette_name="corporate-navy",
        font_pair_name="helvetica-helvetica",
        accent_style="none",
        header_text=None,
        cover_treatment="minimal",
    ),
    "minimal": Template(
        name="minimal",
        description="Editorial / magazine — Times + slate / red accents.",
        palette_name="editorial-slate",
        font_pair_name="times-helvetica",
        accent_style="underline",
        header_text=None,
        cover_treatment="none",
    ),
    "enterprise-dark": Template(
        name="enterprise-dark",
        description="Enterprise dark — enterprise-green-gold + serif body.",
        palette_name="enterprise-green-gold",
        font_pair_name="times-helvetica",
        accent_style="left_bar",
        header_text="AI Gateway Engineering",
        cover_treatment="full",
    ),
}


class TemplateRegistry:
    """Thread-safe template registry, merges builtins + per-tenant overrides."""

    def __init__(self) -> None:
        self._builtins = dict(_BUILTINS)
        self._tenant: dict[tuple[str, str], Template] = {}  # (tenant_id, name) → Template

    def get(self, name: str, *, tenant_id: str | None = None) -> Template:
        if tenant_id is not None and (tenant_id, name) in self._tenant:
            return self._tenant[(tenant_id, name)]
        if name in self._builtins:
            return self._builtins[name]
        raise KeyError(f"unknown template: {name!r}")

    def list(self, *, tenant_id: str | None = None) -> list[Template]:
        out = list(self._builtins.values())
        if tenant_id is not None:
            out.extend([t for (tid, _), t in self._tenant.items() if tid == tenant_id])
        return out

    def register(self, template: Template, *, tenant_id: str) -> None:
        if template.palette_name not in PALETTES:
            raise ValueError(f"unknown palette: {template.palette_name}")
        if template.font_pair_name not in FONT_PAIRS:
            raise ValueError(f"unknown font pair: {template.font_pair_name}")
        self._tenant[(tenant_id, template.name)] = template

    def unregister(self, name: str, *, tenant_id: str) -> bool:
        key = (tenant_id, name)
        if key in self._tenant:
            del self._tenant[key]
            return True
        return False


def default_registry() -> TemplateRegistry:
    return TemplateRegistry()
