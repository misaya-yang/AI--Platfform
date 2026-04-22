"""MCP server wrapping the docgen library.

Phase 2 of the docgen MCP migration. Exposes a single ``generate_document``
tool plus three resource namespaces (design systems, templates, skills).

Transports:
  - stdio  (default — local dev + tests; emits file:// URLs)
  - sse    (deployment — binds Starlette app with /artifacts/{id} route)

Selected via the ``MCP_TRANSPORT`` environment variable.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
