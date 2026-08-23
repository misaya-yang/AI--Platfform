# Agent Runtime source release lock

This directory is the main repository's immutable reference to the separately
maintained upstream Agent Runtime source. Runtime deployment must never follow a
branch, mutable tag, local checkout, or unverified App Server schema.

Files:

- `lock.json` pins source, schema, SBOM, license, and separate App Server plus
  Agent Runtime OCI identities. One artifact can never satisfy the other's gate.
- `source-receipt.json` is generated from one clean fork revision and its
  source-built App Server schema bundle.
- `sbom.cdx.json` is the deterministic CycloneDX inventory for that revision.
- `NOTICE.md` preserves upstream attribution and the exact audited hashes.
- `Dockerfile` builds the Phase 0 source-pinned App Server protocol probe.
- `Dockerfile.runtime` builds the private Rust HTTP/SSE Agent Runtime after the
  controlled fork revision and release receipt are updated together.

Generate source evidence from the independent fork:

```bash
python3 scripts/harness/agent_runtime_supply_chain.py generate \
  --fork /absolute/path/to/agent-runtime-source \
  --schema-dir /absolute/path/to/source-built-schemas \
  --receipt deploy/agent-runtime-source/source-receipt.json \
  --sbom deploy/agent-runtime-source/sbom.cdx.json
```

Then update `lock.json` with the generated hashes and immutable image digest.
`make agent-runtime-source-contract` is the fail-closed admission gate. A source-only
receipt is useful during development but does not permit the Agent Runtime
to start.

Refresh and build a new local release unit only after the controlled fork is
clean and committed:

```bash
python3 scripts/harness/agent_runtime_supply_chain.py refresh-source-lock \
  --repo-root . --lock deploy/agent-runtime-source/lock.json
AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-source-build-local
AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/fork make agent-runtime-build-local
AI_PLATFORM_AGENT_RUNTIME_IMAGE=ai-gateway-agent-runtime:local-<sha> \
  make agent-runtime-smoke
make agent-runtime-contract
```

Refreshing source identity deliberately invalidates both prior images. The two
builders record label-verified local digests atomically; the Runtime contract
requires both artifacts from the same receipt before candidate startup.

The source synchronization policy and kernel decision are recorded in
`docs/architecture/ADR-006-agent-runtime-single-kernel.md`; model, state, and
capability boundaries are in `ADR-007-agent-runtime-data-boundaries.md`.
