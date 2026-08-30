# Handoff

- Active phase: ARU-03
- Active feature: ARU-F004
- Status: working
- Completed: `candidate_verified` — synced upstream `94cbbddafc`, preserved the platform overlay, built Runtime/Worker only through Docker, and verified Gateway, PostgreSQL ThreadStore, Capability Worker, approvals, public Responses/SSE, Agent configuration/Eval, CLI, web, and Qwen compatibility.
- Evidence: Product code `086240f4`; Runtime/Worker `local-94cbbddafc17-46158add036c`; 9 services healthy; Runtime and Worker Docker suites, 148-test Agent configuration/Eval matrix, 16-test Eval contract, 43-test CLI gate, 121-test web unit suite, live Responses 2/2, platform convergence 3/3, Agent Studio preview, Agent publish/Eval/version rollback, history, tool execution/approval, cancellation/recovery, CLI Direct/Chat/tool/resume, and final repository gates passed.
- Next action: After action-time confirmation, use the dedicated ignored E2E identity to sign into the in-app browser and manually click Assistant, approvals, history, Agent Studio, and Eval. Then close ARU-03 and report ARU-04's frozen-bundle blocker.
- Blockers: The official full release-unit rollback is blocked before mutation because the exact immutable frozen Runtime image is absent and four other frozen artifacts are absent or digest-mismatched. A candidate-only Runtime/Worker rollback passed but does not satisfy the frozen-bundle contract.
- Confirmation: Required immediately before entering the dedicated E2E password in the in-app browser.
- Decision: continue

Keep this as the latest checkpoint. Use Git history for older handoffs.
