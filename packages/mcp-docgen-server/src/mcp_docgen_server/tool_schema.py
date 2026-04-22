"""Pydantic schema for the ``generate_document`` MCP tool.

Lives in a dedicated module so the JSON schema can be generated and the
input validated without pulling in ``mcp`` or ``docgen`` — keeps unit
tests fast and lets us diff the schema independently of runtime wiring.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# The six built-in design systems registered in docgen.design_system.
# Kept as an explicit Literal (vs. open string) so MCP clients get
# autocomplete + validation without an extra roundtrip.
DesignSystemName = Literal[
    "claude",
    "stripe",
    "carbon",
    "keynote",
    "editorial",
    "hejaz",
]

DocumentFormat = Literal["docx", "pptx", "xlsx", "pdf"]


class GenerateDocumentInput(BaseModel):
    """Input contract for the single ``generate_document`` tool.

    The tool surface follows the Anthropic / Vercel "fewer tools > more
    tools" principle: one verb, the ``format`` field selects the backend
    renderer. Clients do not need to know which Python module produces
    which format.
    """

    format: DocumentFormat = Field(
        description="Output format. One of: docx, pptx, xlsx, pdf.",
    )
    title: str = Field(
        min_length=1,
        max_length=300,
        description="Document title. Rendered as cover / first heading.",
    )
    goal: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Short statement of what the document is for — audience, "
            "tone, what a reader should walk away with. Drives the planner."
        ),
    )
    body_markdown: Optional[str] = Field(
        default=None,
        max_length=20000,
        description=(
            "Optional markdown body. If present the planner uses it as "
            "ground-truth content instead of generating from scratch."
        ),
    )
    locale: str = Field(
        default="en-US",
        min_length=2,
        max_length=16,
        description="BCP-47 locale (en-US, zh-CN, ar-SA, etc.).",
    )
    design_system: Optional[DesignSystemName] = Field(
        default=None,
        description=(
            "Named design system. Overrides template palette + typography. "
            "Available via the docgen://design-systems resource."
        ),
    )
    template_name: Optional[str] = Field(
        default=None,
        description=(
            "Brand template. Lower precedence than design_system. "
            "Available via the docgen://templates/{tenant_id} resource."
        ),
    )


class GenerateDocumentOutput(BaseModel):
    """Output contract. Returned as a JSON string inside a TextContent."""

    artifact_id: str
    download_url: str
    filename: str
    size_bytes: int
    sha256: str
    plan_outline: str
    critic_passed: bool


def input_json_schema() -> dict:
    """Return the JSON schema for the tool's input.

    Consumed by ``mcp.types.Tool(inputSchema=...)`` at registration time.
    """
    return GenerateDocumentInput.model_json_schema()


def output_json_schema() -> dict:
    """Output schema — used for documentation; MCP does not enforce it."""
    return GenerateDocumentOutput.model_json_schema()
