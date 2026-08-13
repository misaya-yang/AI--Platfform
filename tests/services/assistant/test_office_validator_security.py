from __future__ import annotations

import importlib.util
import py_compile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR_ROOT = (
    REPO_ROOT
    / "packages"
    / "mcp-docgen-server"
    / "src"
    / "docgen"
    / "_skills_data"
)


def _validator_path(skill: str, module: str) -> Path:
    return VALIDATOR_ROOT / skill / "scripts" / "office" / "validators" / f"{module}.py"


def test_all_office_validator_modules_compile():
    for skill in ("docx", "pptx", "xlsx"):
        for module in ("base", "docx", "pptx"):
            py_compile.compile(str(_validator_path(skill, module)), doraise=True)


@pytest.mark.parametrize("skill", ["docx", "pptx", "xlsx"])
def test_office_validator_parser_does_not_expand_external_entities(skill, tmp_path):
    module_path = _validator_path(skill, "base")
    spec = importlib.util.spec_from_file_location(
        f"office_{skill}_validator_base",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    marker = "external-entity-must-not-be-expanded"
    entity_file = tmp_path / "entity.txt"
    entity_file.write_text(marker, encoding="utf-8")
    payload = (
        '<!DOCTYPE root [<!ENTITY xxe SYSTEM "' + entity_file.as_uri() + '">]><root>&xxe;</root>'
    )

    root = module.lxml.etree.fromstring(
        payload.encode(),
        module._SAFE_XML_PARSER,
    )
    rendered = module.lxml.etree.tostring(root, encoding="unicode")

    assert marker not in rendered
