"""Export the full API route contract (path/method/operation_id/status codes).

Used by ARC-01/ARC-01B to prove zero drift before/after the route split.
Dumps two views of the same app:
  1. flattened route walk (path, methods, operation_id, status_code, responses)
  2. OpenAPI view (paths -> methods -> operationId + response codes)
"""
from __future__ import annotations

import json
import sys


def _walk(routes, out, prefix: str = ""):
    from fastapi.routing import _IncludedRouter

    for route in routes:
        if isinstance(route, _IncludedRouter):
            inner = getattr(route, "original_router", None)
            if inner is not None:
                ctx = getattr(route, "include_context", None)
                inc_prefix = (getattr(ctx, "prefix", "") or "") if ctx is not None else ""
                _walk(inner.routes, out, prefix + inc_prefix)
            continue
        if not hasattr(route, "methods"):
            continue
        out.append(
            {
                "path": prefix + route.path,
                "methods": sorted(route.methods or []),
                "operation_id": getattr(route, "operation_id", None),
                "status_code": getattr(route, "status_code", None),
                "responses": sorted(
                    {str(code) for code in (getattr(route, "responses", None) or {})}
                ),
            }
        )


def export() -> dict:
    from src.main import app

    rows: list[dict] = []
    _walk(app.routes, rows)
    rows.sort(key=lambda r: (r["path"], r["methods"]))

    openapi = app.openapi()
    oa_rows = []
    for path, item in openapi.get("paths", {}).items():
        for method, op in item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            oa_rows.append(
                {
                    "path": path,
                    "method": method,
                    "operation_id": op.get("operationId"),
                    "response_codes": sorted(op.get("responses", {}).keys()),
                }
            )
    oa_rows.sort(key=lambda r: (r["path"], r["method"]))
    return {"routes": rows, "openapi": oa_rows}


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "tmp/api-routes.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(export(), fh, indent=1, sort_keys=True)
    print(f"wrote {out}")
