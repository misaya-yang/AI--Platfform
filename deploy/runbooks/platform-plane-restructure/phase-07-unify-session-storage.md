# Phase 07 - Unify session storage

- PHASE_ID: PPR-07
- FEATURE_ID: PPR-F008
- DEPENDS_ON: PPR-06

## Outcome

Conversation history has one normalized home. The gateway path stops writing the unbounded `sessions.history` JSONB column, and reads project from the same item store the runtime already owns.

## Starting position (verified 2026-08-26)

The runtime path is already normalized: `assistant_runtime_items` holds 33,472 rows against 941 sessions. `sessions.history JSONB` (schema.sql:236) survives only for the V1/gateway compatibility surface — an unbounded column that grows without a tail bound.

## Scope

In:

- Gateway session reads/writes project from `assistant_runtime_items`.
- A migration that backfills or explicitly tombstones legacy `history` payloads, with a reversible plan.
- Retain the audit chain semantics: `ON DELETE RESTRICT` and the tombstone-on-delete behaviour must not change.

Out:

- Changing the public session API shape.
- Deleting historical data. Legacy rows are migrated or frozen, never dropped.

## Done when

- [ ] No gateway write path targets `sessions.history`.
- [ ] Legacy sessions still render their history identically through the public API.
- [ ] The migration is reversible and rehearsed on a copy before it touches the live database.
- [ ] Session delete still tombstones (never violates the runtime FK) — the 2026-08-26 regression tests still pass.
- [ ] `make migrate-status` is clean; `database/schema.sql` matches the migrations.
- [ ] Full regression passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| No legacy writes | Grep + query audit during the live suite | Column is read-only, then unused |
| Render parity | Public history API before/after on legacy sessions | Identical payloads |
| Delete safety | `tests/persistence/test_session_delete_tombstone.py`, `tests/api/test_sessions_runtime_cleanup.py` | FK and tombstone intact |
| Migration | `make migrate-status`, rehearsal on a copy | Reversible and consistent |

## Stop or confirm

- **Confirm with the user before running any migration against real data.**
- Stop if render parity cannot be shown for legacy sessions; leave the column in place and record why.
