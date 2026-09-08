# Agent Runtime source release lock

This directory is the main repository's immutable reference to the separately
maintained upstream Agent Runtime source. Runtime deployment must never follow a
branch, mutable tag, local checkout, or unverified App Server schema.

Files:

- `lock.json` pins source, schema, SBOM, license, and separate App Server,
  Agent Runtime and capability worker OCI identities. One artifact cannot satisfy another's gate.
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

Refreshing source identity invalidates every prior image. The existing
`scripts/rust/build-update.sh --artifact all` builds Runtime and capability worker
serially, then checks both selected images against the source lock. App Server
remains a separate protocol-probe artifact.

`make quickstart-build` and `scripts/new/deploy.sh --build` use both existing Rust
builders before Compose builds the Python/Web images. A prebuilt Runtime never
substitutes for a missing Worker. Deployment verifies the actual selected image
IDs, platform, source/schema/binary labels and the Worker's full overlay hash
before stopping or starting application services:

```bash
python3 scripts/harness/agent_runtime_supply_chain.py verify-local-images \
  --repo-root . --lock deploy/agent-runtime-source/lock.json \
  --runtime-image <runtime-tag-or-digest> --worker-image <worker-tag-or-digest>
```

Source hot-update uses the same pair check and compares container image IDs,
so replacing an image behind the same tag still recreates the Runtime/Worker
pair. Existing stopped Python containers can receive Gateway/Knowledge source
and both shared packages without starting the old application: site-packages is
queried by an isolated `python` entrypoint from that container's immutable image.
The frontend entrypoint is copied with execute permission, then runs on the final
restart. `--no-restart` copies files without starting services or probing app health.
Hot-update does not rewrite container environment values; deployment requires any
explicit kernel revision to match the selected source unit before building or
changing application services.

Source identity checks do not claim that native execution, Docker health,
provider calls, rollback, publication or another platform has been verified.

When the platform overlay or capability migration changes, refresh its derived
identity and Worker dependency SBOM from the clean, controlled composed source
workspace. This command only reads that workspace; it does not modify it:

```bash
python3 scripts/harness/agent_runtime_supply_chain.py refresh-overlay \
  --repo-root . \
  --lock deploy/agent-runtime-source/lock.json \
  --cargo-workspace /absolute/path/to/composed/codex-rs
```

The source synchronization policy and kernel decision are recorded in
`docs/architecture/ADR-006-agent-runtime-single-kernel.md`; model, state, and
capability boundaries are in `ADR-007-agent-runtime-data-boundaries.md`.
