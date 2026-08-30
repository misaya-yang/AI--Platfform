# HANDOFF — platform-architecture-convergence

## Where the program stands (2026-08-29)

- **Accepted architecture:** ADR-008 (three bounded contexts, polyglot, no new
  resident services, same-database role separation). ADR-006/007 invariants
  retained; obsolete migration items superseded item-by-item.
- **Facts:** `docs/architecture/baselines/2026-08-post-rag/` (six JSON
  baselines incl. `contract-freeze.json`). Regenerate:
  `python3 scripts/inventory/generate_baselines.py`; check drift: add
  `--verify`. Deterministic at a fixed Git revision — if `--verify` reports
  drift while other agents are mid-work, wait for the tree to settle and
  regenerate; drift at a settled tree means the baselines must be refreshed
  in the same commit set as the change that caused it.
- **Ledgers:** FRC terminal; PPR superseded (per-phase disposition inside its
  loop-state); PCH/ACU/CHR/ARS/AH/AGAH already superseded. Exactly one active
  architecture program: this one. kb-rag-ui-t5 remains a frontend feature
  ledger (not architecture); sota-performance-dual-gate remains a blocked
  performance program (not architecture).
- **Packages:** see `work-packages.yml`. Under explicit user direction,
  ARC-00A/00B/00C/01/02/02B/03/04 run in parallel in the same directory with
  mutually exclusive owned paths; the primary session performs all commits.

## Do not

- Do not edit protected integration paths outside a named integration commit
  (`docs/harness/work-packages.md` §4): src/main.py, src/api/router.py,
  docker-compose*.yml, Makefile, harness.yml, .env.example,
  .github/workflows/**, docs/README.md, database/schema.sql.
- Do not rewrite historical reports; status changes only.
- Do not count fixture/offline evidence as live results (PRD AC-M19/M20).
- Do not resume FRC or PPR; their follow-ups are owned here.

## Resume steps

1. `bash deploy/runbooks/platform-architecture-convergence/init.sh`
2. Read `work-packages.yml` for the package you are picking up; check its
   `stop_conditions` before acting.
3. On reaching `direct_verified` or later, write
   `receipts/<package-id>.yml` in the §7 format.
