# Handoff

- Active phase: CHR-02
- Active feature: CHR-F003
- Status: working
- Completed: CHR-01 is closed. The clean fork revision `44d926ab7c9e...` is source-locked; App Server and Agent Runtime have separate digest-pinned local images; the Rust Runtime owns durable Thread lifecycle and V1 projection without a second Agent loop; Gateway assignment remains prompt-agnostic and fail-closed before candidate Turn routing.
- Evidence: `make codex-thread-store-contract` passed Python 4, Rust 6, and live two-process contract 1. Gateway/session/supply-chain tests passed 29/29. `make codex-runtime-contract` passed both artifact identities. The isolated Docker smoke created a Thread, killed the first Runtime, resumed it in a second process, and observed 36.6 MB then 14.0 MB cgroup memory. Ruff, Harness, config, Compose, Dockerfile, Bazel lock, and negative admission gates passed.
- Next action: Implement CHR-02's immutable RuntimeSnapshot and short-lived signed lease, then add the Gateway's strictly private model-only Responses stream and a pure-text Codex Turn gate without entering the Python AgentLoop.
- Blockers: none
- Confirmation: none
- Decision: continue

Keep this as the latest checkpoint. Use Git history for older handoffs.
