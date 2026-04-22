# hejaz-docgen

Design-system-driven document generation for Microsoft Office and PDF formats
(docx / pptx / xlsx / pdf). The LLM plans a typed pydantic Intermediate
Representation (IR); deterministic renderers emit the file bytes. A vision
critic closes the loop on PPTX quality.

## Architecture (7 layers)

- `skills/` — intent router, prompt templates, skill registry
- `ir/` — pydantic IR: root docs (docx/pptx/xlsx/pdf) + shared base blocks
- `planners/` — format-specific planners that turn briefs into IR
- `renderers/` — IR -> OOXML/PDF via python-pptx, python-docx, openpyxl, reportlab
- `quality/` — structural verifier + vision critic (layout bounds, fonts, overflow)
- `sandbox/` — local + docker execution backends for rendering isolation
- `storage/` — local fs + S3 + signed URLs for rendered artifacts

## Install

```bash
pip install -e .
# with vision critic + test deps
pip install -e ".[vision,dev]"
```

## Minimal usage

```python
from docgen.service import DocgenService, GenerateRequest

svc = DocgenService()
result = svc.generate(GenerateRequest(
    kind="pptx",
    brief="5-slide investor pitch on coastal logistics",
))
print(result.artifact_uri)
```

## Hard constraints

- The pydantic IR is the only contract between planner and renderer.
- No LLM ever emits raw OOXML / XML / HTML — planners fill IR, renderers render.
- Rendering runs sandbox-first (local subprocess or docker) so a bad IR can
  never crash the host service.

## MCP wrapper

An MCP server that exposes the same capabilities over stdio lives at
`packages/mcp-docgen-server/src/mcp_docgen_server/` (authored in parallel by
Agent B). This package is the business-logic core; the MCP layer only wraps
`DocgenService`.
