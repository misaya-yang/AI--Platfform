# Phase 01 - Make Rust build and distribution cheap

- PHASE_ID: PPR-01
- FEATURE_ID: PPR-F002
- DEPENDS_ON: PPR-00

## Outcome

Changing one line of Rust reaches a running container through one command in under 15 minutes, the runtime image is a normal service image rather than a toolchain image, and changing one crate does not rebuild the other.

## Why this is second

Every later Rust decision is multiplied by this friction. Today: the runtime image is **2.36 GB** because `deploy/agent-runtime-source/Dockerfile.runtime:39` runs `FROM rust:1.95.0-bookworm` for the runtime stage; image identity is duplicated across `lock.json`, `.env`, `.env.example` and `docker-compose.yml` (two services); and `rust/agent-runtime-overlay/manifest.json` carries a single whole-tree `sha256`, so editing the capability worker changes the agent-runtime tag too.

## Scope

In:

- Runtime stage on a slim/distroless base for `Dockerfile.runtime`; keep the capability worker on a Python base only for what `execute_python_code` genuinely needs.
- One command that builds, records into the lock, and re-derives every downstream pin from the lock.
- Per-crate overlay identity so each artifact tag tracks only its own sources.

Out:

- Any behaviour change in the binaries.
- Changing the supply-chain proof semantics (lock / SBOM / receipt remain authoritative).

## Done when

- [ ] `ai-gateway-agent-runtime` image ≤ 150 MB and the container still passes its healthcheck and the live suite.
- [ ] One command performs build → `record-local-image` → re-derive `.env`, `.env.example`, and Compose defaults **from `lock.json`**; no file holds an independently editable copy of an image tag.
- [ ] Editing a file under `ai-platform-capability-worker/` and rebuilding leaves the `ai-platform-agent-runtime` tag unchanged.
- [ ] `make agent-runtime-source-contract` passes, including the SBOM-describes-the-locked-overlay check.
- [ ] Measured wall time from a one-line Rust edit to a healthy container is ≤ 15 min, recorded in the report.
- [ ] Full regression passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Image size | `docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' \| grep agent-runtime` | ≤ 150 MB |
| Single authority | Grep the tree for a hardcoded `local-<sha>-<sha>` outside `lock.json` | Pins are derived, not copied |
| Crate isolation | Touch a worker source file, rebuild both, compare tags | Runtime tag unchanged |
| Supply chain | `make agent-runtime-source-contract` | Lock, SBOM, receipt and image identity agree |
| Round trip | Timed one-line edit → healthy container | ≤ 15 min |

## Stop or confirm

- Confirm before changing the runtime base image family — it changes the deployed attack surface.
- Stop if a slim base breaks the entrypoint or healthcheck rather than working around it with a fat base.
