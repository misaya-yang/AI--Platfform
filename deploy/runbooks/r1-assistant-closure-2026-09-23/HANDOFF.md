# R1 / durable recovery handoff

Authoritative status: [loop-state.json](loop-state.json).
DR-01 local acceptance and integration to `main` are complete. Source branch
`codex/runtime-durable-recovery`, implementation commit `91eadb92`.
Acceptance base `92c98c0b85c7b9b72254e5023e6086fd8d4792df`. Local fast-forward
main integration performed after user authorization; no remote push.
Preexisting dirty PRD, docs index and another thread's architecture planning
remain preserved. Primary writer only; all other agents were read-only. Post-commit SQL two-role
matrix and source-contract passed; unrelated files remained byte-identical.

## Final local runtime

- C4 overlay `22bf4e1ecad5`, upstream `279ba894152b`.
- Exact Runtime/Worker local images at that suffix; source lock records OCI digests.
- Actual local authority epoch4 applied additively; no data cleanup or rollback.
- Gateway hot-updated final Python; frontend unchanged this round.
- Compose owner matches this checkout; validate/status/source-contract passed.
- Controlled acceptance fault files absent. Real Quiz/history/receipts retained.
- Built-in browser remains at the real DR-C4 completed original task.

## Proof and boundary

[Report](../../../reports/product/runtime-durable-recovery-2026-09-26.md) and
[DR-01 receipt](receipts/DR-01.yml) contain actual commands and separate automated,
isolated-DB, real-browser and controlled-crash evidence. Runtime/Worker/Core
160 Rust tests passed (2 explicit optional PG skips); Python200, live isolated
role matrix2; six successful crash windows + reject/cancel/revoke/unknown,
and final real Qwen pending approval SIGTERM original-run continuation passed.

Unknown writes never redispatch. Expired/revoked authority does not extend.
Native/unprovable descendants and old runs without durable context fail closed.
No hosted CI, whole-platform HA, Gateway restart continuity or complete R1
claim. Remaining R1 journeys stay in loop-state's separate unverified blocker.
No further DR-01 development is required unless a new concrete defect is reported.
