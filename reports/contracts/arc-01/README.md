# ARC-01/01B contract evidence — Assistant API route surface snapshots

- `assistant-api-routes-before.json` — in-process route-surface export taken before the ARC-01 split.
- `assistant-api-routes-after.json` — same export after the split; zero drift vs `before`.
- `assistant-api-routes-now.json` — re-verification export at integration time.
- `arc01-export-routes.py` — the exporter (`uv run python <script> <out.json>`); dumps a flattened
  route walk plus the OpenAPI view.

The authoritative drift check is the OpenAPI view (see `scripts/harness/openapi_contract_gate.py`);
the route-walk view is a supporting witness only (it carries a prefix-traversal artifact noted in
the ARC-01 report).
