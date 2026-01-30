# Old Tools (Deprecated)

These Python scripts have been archived. Most were one-off debug/diagnostic tools.

## Scripts with issues:
- `diagnose_auth.py` — Contains hardcoded Windows paths (`C:/Projects/Agent_Gateway`)
- `reprocess_images.py` — Hardcoded page IDs, document IDs, dataset IDs; single-use only
- `dedupe_via_api.py` — Hardcoded search queries, not reusable

## Potentially useful (if cleaned up):
- `reset_admin_password.py` — Admin password reset (works, but also accessible via `make dev-setup`)
- `db_check.py` — Quick database inspection
- `redis_check.py` — Redis connection verification
- `dedupe_segments.py` — Knowledge base deduplication (generic, takes dataset_id arg)
