# Handoff

- Active phase: PPR-00
- Active feature: PPR-F001
- Status: authored, not started
- Completed: Program authored 2026-08-26 from a line-by-line survey of the current tree. Six items on the 2026-08-17 SPO list were verified as already fixed (single-EVAL admission, dimension-skip rate limiting, pure-ASGI security headers, single JWT verification, cached `get_collection`, ingest/retrieve process split) and four more are obsolete after the Python AgentLoop deletion. All ten are excluded in `product-requirements.md` §2.5.
- Evidence: `docs/plans/rust-expansion-and-service-topology-2026-08.md` §2.4–2.5; `reports/performance/assistant-ttft-*.json` (four same-config probes: local 14–19 ms, TTFT p50 3.925–9.281 s).
- Next action: Start PPR-00 — split `local_overhead_seconds` from `ttft_seconds` in `scripts/assistant_ttft_benchmark.py`, characterise provider variance at N ≥ 30, gate only the local SLI, and freeze the program's rollback bundle.
- Blockers: none
- Confirmation: none — PPR-07 (migration against real data) and PPR-09 (image swap) will need one when reached.
- Decision: continue

Keep this as the latest checkpoint. Use Git history for older handoffs.
