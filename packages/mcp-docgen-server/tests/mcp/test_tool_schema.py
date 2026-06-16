"""Schema shape tests for ``generate_document`` — no network, no MCP import.

These are the one set of tests that can run RIGHT NOW (before Agent A
lands), because ``tool_schema`` only depends on pydantic.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError


# Import path assumes ``PYTHONPATH=packages/mcp-docgen-server/src`` or
# that the package is editable-installed.
from mcp_docgen_server.tool_schema import (
    GenerateDocumentInput,
    input_json_schema,
    output_json_schema,
)


# ---------------------------------------------------------------------------
# Happy path


def test_minimal_valid_input() -> None:
    m = GenerateDocumentInput(format="pptx", title="Q2 review", goal="Executive summary")
    assert m.format == "pptx"
    assert m.locale == "en-US"
    assert m.body_markdown is None


def test_all_fields_valid_input() -> None:
    m = GenerateDocumentInput(
        format="docx",
        title="Design doc",
        goal="Specify the MCP server",
        body_markdown="# Intro\n\nHello.",
        locale="zh-CN",
        design_system="claude",
        template_name="enterprise-dark",
    )
    assert m.design_system == "claude"
    assert m.locale == "zh-CN"


# ---------------------------------------------------------------------------
# Rejection


def test_rejects_bogus_format() -> None:
    with pytest.raises(ValidationError):
        GenerateDocumentInput(format="bogus", title="x", goal="y")  # type: ignore[arg-type]


def test_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        GenerateDocumentInput(format="pdf", title="", goal="y")


def test_rejects_oversize_body_markdown() -> None:
    too_long = "x" * 20_001
    with pytest.raises(ValidationError):
        GenerateDocumentInput(format="pdf", title="ok", goal="ok", body_markdown=too_long)


def test_rejects_bogus_design_system() -> None:
    with pytest.raises(ValidationError):
        GenerateDocumentInput(
            format="pdf",
            title="x",
            goal="y",
            design_system="not-a-system",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# JSON schema contract


def test_json_schema_exposes_four_formats() -> None:
    schema = input_json_schema()
    assert "format" in schema["properties"]
    # pydantic emits the enum via $ref or inline depending on version,
    # so walk the resolved schema defensively.
    formats = _collect_enum_for_field(schema, "format")
    assert sorted(formats) == ["docx", "pdf", "pptx", "xlsx"]


def test_json_schema_exposes_six_design_systems() -> None:
    schema = input_json_schema()
    systems = _collect_enum_for_field(schema, "design_system")
    assert sorted(systems) == ["carbon", "claude", "editorial", "enterprise", "keynote", "stripe"]


def test_json_schema_is_dumpable() -> None:
    schema = input_json_schema()
    blob = json.dumps(schema)
    assert "generate" not in blob.lower() or "GenerateDocument" in blob  # smoke
    assert len(blob) > 100


def test_output_schema_fields_match_contract() -> None:
    schema = output_json_schema()
    assert set(schema["properties"]) == {
        "artifact_id",
        "download_url",
        "filename",
        "size_bytes",
        "sha256",
        "plan_outline",
        "critic_passed",
    }


# ---------------------------------------------------------------------------
# helpers


def _collect_enum_for_field(schema: dict, field: str) -> list[str]:
    """Resolve the enum members for ``field`` regardless of $ref indirection."""
    prop = schema["properties"][field]
    # Direct enum
    if "enum" in prop:
        return list(prop["enum"])
    # Optional[Literal[...]] → anyOf: [ {ref}, {null} ] or {type:null}
    candidates = []
    if "anyOf" in prop:
        candidates.extend(prop["anyOf"])
    if "$ref" in prop:
        candidates.append(prop)
    defs = schema.get("$defs") or schema.get("definitions") or {}
    for c in candidates:
        ref = c.get("$ref")
        if not ref:
            if "enum" in c:
                return list(c["enum"])
            continue
        name = ref.rsplit("/", 1)[-1]
        target = defs.get(name, {})
        if "enum" in target:
            return list(target["enum"])
    return []
