# Handoff

- Active phase: CHR-02
- Active feature: CHR-F003
- Status: working
- Completed: CHR-01 is closed. CHR-02 now has immutable Runtime snapshots, signed scope-bound model leases, exact platform Run/Codex Turn identity, a private Gateway model-only Responses plane, and native Qwen Responses as the data-driven default. Explicit tenant Chat Completions remains a compatibility fallback.
- Evidence: the canonical real-Qwen gate covers exact simple output, a long Transformer explanation, remember/recall, one model-call accounting path, TTFT, terminal uniqueness, Runtime restart, and Thread resume. The old locked image exposed a real restart bug by selecting `api.openai.com`; the current fix replays only model ID and a validated platform model-plane URL, uses `exclude_turns`, and issues no lease when resume fails. Current Rust Runtime tests pass 10/10, the affected Python cohort passes 157/157, the development image passes Docker lifecycle smoke, and the four real Qwen Turns pass with first-visible 3.865/1.978/1.665/1.959 seconds, exactly four model calls, 1,755 long-output characters, and 47.5 MB Runtime memory.
- Next action: obtain explicit authorization for local checkpoint commits in both worktrees, rebuild through the immutable supply-chain target, refresh source/SBOM/OCI receipts, and rerun the same gates. Only then mark CHR-02 done and start CHR-03. No push or publication is requested.
- Environment incident: Docker recovered after deleting only the 31GB reproducible Rust incremental cache and performing a normal Docker Desktop restart. PostgreSQL, Redis, Qdrant, network writes, BuildKit, and isolated smokes are healthy again. No prune, volume deletion, Docker reset, or source deletion occurred.
- Blockers: none
- Confirmation: none
- Decision: continue

Keep this as the latest checkpoint. Use Git history for older handoffs.
