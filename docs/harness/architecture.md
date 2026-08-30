# Architecture and Boundaries

> Current structural contract for coding agents. This file separates facts that are true today,
> target boundaries that are queued, and named exceptions that must not be mistaken for accepted
> architecture.

**Schema:** `harness/architecture/v2`
**Last verified:** 2026-08-29 against `main@47b7a9b` (before the RAG worktree is merged)

The RAG branch changes Knowledge routes, worker behaviour, database migrations, Gateway KB access,
Compose and harness files. ARC-00 of the now-active architecture convergence program
(branch `platform-arch-convergence-2026-08`, post-RAG base `336851c1`) re-verifies every fact
here; machine-readable post-RAG facts live in `docs/architecture/baselines/2026-08-post-rag/`.

---

## 1. Observed operational groups and deployment roles

The current call/data topology suggests three business contexts, one product surface, and shared
infrastructure. Their observed process roles are current fact; formal bounded-context acceptance
was granted by [`ADR-008`](../architecture/ADR-008-bounded-contexts-no-new-services.md)
(Accepted 2026-08-29). A logical module or process role does not automatically deserve a new
service.

| Context | Compose service / role | Language | Current responsibility | Exposure | Current scale truth |
| --- | --- | --- | --- | --- | --- |
| Surface | `frontend` / `ai-gateway-frontend` | TS/nginx | Web console and public static assets | `127.0.0.1:8081` | Stateless; scalable behind an external edge |
| Gateway Control | `gateway` / `ai-gateway-backend` | Python | Public API, auth, tenant policy, provider/model plane, quota/billing, compatibility facades | `127.0.0.1:8080` | **Single instance today**: in-process schedulers/workers lack leader ownership |
| Agent Execution | `agent-runtime` / `ai-gateway-agent-runtime` | Rust | Single Thread/Turn kernel, context/compaction, approval lifecycle/decision, event order, interruption and projection | Docker-internal `:8094` | **Single instance today**: in-process broadcast/ownership lacks cross-instance affinity |
| Agent Execution | `agent-capability-worker` / `ai-gateway-agent-capability-worker` | Rust | Capability catalog/execution/events, artifact/tool dispatch; validates bound one-time approval receipts before side effects | Docker-internal | Designed for worker replication, but release gates must prove multi-replica recovery/cancellation |
| Knowledge | `knowledge-service` / `ai-gateway-knowledge-service` | Python | Dataset/document API, retrieval, embedding/rerank control | `127.0.0.1:8092` | API role can be separated from workers |
| Knowledge | `knowledge-worker` / `ai-gateway-knowledge-worker` | Python | Durable ingestion/indexing/background work from the same code unit | Docker-internal | Durable claim supports replication; re-verify after RAG merge |

One-shot jobs are not long-running business services:

| Job | Purpose |
| --- | --- |
| `gateway-init` / `ai-gateway-init` | Expected-to-exit bootstrap/permission initialization |
| `migrate` / `ai-gateway-migrate` | Profile-gated database migration job |

Shared infrastructure consists of PostgreSQL, Redis, Qdrant, object storage, and optional Tempo.
The Compose project name, network and container names are currently fixed, so local Docker is a
singleton shared by every worktree. See [`integration-and-rollback.md`](integration-and-rollback.md).

## 2. Current runtime call graph

Runtime calls are not Python import permissions.

```text
Browser / SDK
  -> Frontend/nginx
  -> Gateway public API
       -> Rust Agent Runtime (start/resume/cancel + SSE)
            -> Gateway private model plane -> configured provider
            -> Rust Capability Worker
                 -> Knowledge private retrieval
                 -> Gateway-owned brokers (image/MCP/connector/artifact/local node)
       -> Knowledge public proxy -> Knowledge API
                                    -> PostgreSQL / Qdrant / object storage
                              Knowledge Worker -> PostgreSQL / Qdrant / object storage
```

Allowed synchronous edges:

| Caller | Callee | Purpose | Must not become |
| --- | --- | --- | --- |
| Frontend/SDK | Gateway public API | User/product contract | Direct Runtime, Worker or Knowledge access |
| Gateway | Runtime | Turn lifecycle and event stream | A second Agent loop |
| Runtime | Gateway private model plane | One authorized provider call | Provider credentials or billing in Runtime |
| Runtime | Capability Worker | Signed, scoped capability execution | Model/tool-selection loop in Worker |
| Worker | Knowledge | Tenant/resource-bound retrieval and data actions | Direct DB access to Knowledge tables |
| Worker | Gateway broker | Gateway-owned provider/connector operations | Public user API bypass |
| Gateway | Knowledge | Public management proxy and explicit authorization | Python import or direct KB-table ownership |

Current exception: Gateway control plane fetches the capability catalog through its own
`gateway:8080` internal route before that route calls Worker. The active architecture convergence
program (ARC-02) replaces only this Gateway→Gateway loopback with an in-process service while
preserving tenant, RBAC, policy and Worker validation.

## 3. Code dependency graph

The Python packages are siblings over a shared leaf; they are not a linear import chain:

```text
web --HTTP--> src (Gateway)
                    \
                     -> packages/ai-gateway-core (temporary shared leaf)
                    /
apps/* -------------

src  -X-> apps/*
apps -X-> src
appA -X-> appB
core -X-> src or apps
```

Hard rules:

1. `src/api/` owns HTTP binding and public schemas, not reusable business services.
2. `src/services/` owns Gateway services; it must not import an application implementation.
3. `apps/*` must not import `src/` or another application.
4. `packages/ai-gateway-core` must not import `src/` or `apps/*`. Domain implementations inside the
   current core are named debt, not permission to add more.
5. `web/` consumes Gateway public contracts; generated SDK clients belong in `sdk/`.
6. Cross-language Runtime/Worker contracts use versioned JSON Schema/fixtures or Rust authority;
   do not hand-maintain divergent Python and Rust constants.

The repository currently lacks a real static import-boundary gate. `make test-isolation` does not
prove rules 2–4 and may pass with its live OpenAPI check skipped. ARC-00B of the active convergence
program adds an offline boundary and in-process OpenAPI gate before later refactors rely on it.

## 4. Data ownership

### 4.1 Current fact

- All application processes currently share an over-privileged PostgreSQL login and broad
  `search_path`; named schemas are not effective authorization boundaries.
- Runtime/Capability tables and Gateway tables are not cleanly separated by schema; access must be
  inventoried at table/function/sequence level before roles are restricted.
- Qdrant is shared infrastructure: Knowledge owns dataset collections, while Gateway/Agent data
  governance currently owns Agent memory-vector namespaces.
- Redis key ownership and optional/durable dependency semantics are not yet mechanically declared.
- Knowledge owns dataset object-store generations; artifacts and other Gateway-owned objects require
  separate namespaces and credentials.

### 4.2 Target invariant

Every persistent object has exactly one writer owner, a list of legal readers, a migration owner,
and a tenant-isolation test. PostgreSQL roles are per process, not shorthand for “one whole schema.”
Qdrant and object storage use explicit collection/bucket/prefix ownership with negative cross-domain
tests. A DB role does not replace tenant predicates or object-level authorization.

The post-RAG data inventory and role rollout belong to ARC-03/04 of the queued plan; they are not
implemented invariants yet.

## 5. Product and execution boundaries

- Rust Agent Runtime is the only active Agent loop. The deleted Python AgentLoop, Assistant Service,
  docgen executor and Python fallback may not return.
- Gateway may resolve identity, tenant policy, model access, an immutable Agent launch/snapshot and
  provider credentials. It must not own model/tool iteration, compaction, cancellation state, or
  event ordering.
- Capability Worker executes capabilities and durable side effects; it does not select the next
  model action.
- Knowledge owns ingestion, parsing, embedding, retrieval and dataset lifecycle; Gateway brokers its
  public surface but does not import or directly write Knowledge tables.
- Default Assistant, Responses, Studio Preview and Published Agent should converge on one versioned
  resolved-launch contract. This is a queued target; current entry paths still assemble snapshots
  differently.

## 6. Contracts that must not drift silently

| Contract | Current source of truth | Required proof | Current gap |
| --- | --- | --- | --- |
| Public OpenAPI | `sdk/openapi.json` + application routes | In-process snapshot comparison; live route check separately | Existing live check may skip and still return green |
| Assistant/Runtime SSE | versioned fixtures and SDK contract | Python/CLI/Java/Dart plus browser projection | Gate coverage must be tied to changed paths |
| Runtime HTTP/thread contract | Rust Runtime API + fixtures | Rust tests, Gateway adapter tests, restart recovery | Current CI runs no general Rust workspace tests |
| Private model plane | Gateway service + internal route | Auth/scope/provider/accounting/timing contracts | Large module and manual trigger mapping |
| Capability descriptor/execution | Rust `CapabilityDescriptorV2`, leases/proofs/events | Cross-language fixture and Runtime↔Worker tests | Current V2→V1→V2 projection duplicates validation |
| Database | migration history, runner ledger, supported init paths | Fresh/upgrade/adoption/checksum/fingerprint/role matrix | Multiple runners and ledgers; no single CI authority |
| Health and degradation | `/health`, `/health/ready`, dependency detail | Fault injection with product-correct terminal state | Several probes are shallow or classify optional dependencies as core |
| Trace identity | W3C trace + request/run/turn/execution ids | Hop-by-hop fixture and log-safety test | Not yet end-to-end enforced |
| Release unit | compatibility manifest and `/version` surfaces | Digests/revisions actually served by every process | Manifest and multi-arch Runtime/Worker distribution not complete |

Until the listed gap is closed, documentation must say `planned` or `structural check`, not “machine
enforced.”

## 7. Named architecture debt

| Debt | Consequence | Owning queued package |
| --- | --- | --- |
| False-green TypeScript/OpenAPI/isolation and incomplete CI | Refactors can merge without exercising the claimed code | ARC-00 |
| Runtime/Worker local image build is expensive and distribution is not truly multi-arch | Low-memory developers cannot iterate or fresh-install reliably | ARC-00C / final release |
| Oversized Assistant/Agent API/control/model/Rust HTTP modules | Ownership is hard to locate and review | ARC-01/02 |
| Assistant/Studio launch paths differ | “Assistant is an AgentSpec instance” is not fully true | ARC-02 |
| Capability V2 repeatedly projected and per-tool catalog preflight duplicated | Drift risk and unnecessary hops | ARC-02 |
| Multiple DB runners/ledgers and weak grants | Schema drift and cross-domain writes | ARC-03/04 |
| Synchronous long Knowledge backfill/gate operations | HTTP timeout/cancellation ambiguity | RAG acceptance + ARC long-job package |
| Shallow readiness and Worker outage kills default pure text | False health and wrong degradation | ARC-05 |
| Gateway/Runtime incorrectly described as horizontally scalable | Duplicate schedulers or lost live events | ARC-05/06 |
| Fixed Compose identity and scripts omit Capability Worker | Worktree/runtime confusion and incomplete Agent release | ARC-06/final release |
| Stale plans/tests/docs/artifacts and advisory size limits | False guidance and accumulating repository debt | Repository-quality package |

## 8. Size and hygiene guardrails

Line count is a review trigger, not proof of architecture quality.

| Language | Review split | Existing no-growth threshold | New module target |
| --- | --- | --- | --- |
| Python | ≥1000 | ≥1500 | <800 |
| TS/TSX | ≥500 | ≥1000 | <500 |
| Rust | Ownership/cyclomatic review by crate/module | Named baseline after RAG | Prefer one protocol/use-case group |

After the RAG merge, record a machine-readable baseline. Existing over-threshold files may not grow
without a dated exception, owner and removal condition. Pure file splitting is not sufficient: the
new import graph and owner must be clearer and the behavioural contract must remain stable. Follow
[`repository-quality.md`](repository-quality.md).

## 9. Architecture decisions

ADRs live in `docs/architecture/`. Accepted ADRs preserve decision history. When implementation
invalidates a rollback path or owner statement, a successor ADR supersedes the precise clause; do
not silently rewrite the old decision.

Current key decisions:

- [ADR-004 — bounded plugin subagent delegation](../architecture/ADR-004-bounded-plugin-subagent-delegation.md)
- [ADR-005 — tenant-configurable model capability profiles](../architecture/ADR-005-model-capability-profiles.md)
- [ADR-006 — Agent Runtime single target kernel](../architecture/ADR-006-agent-runtime-single-kernel.md)
- [ADR-007 — Agent Runtime data boundaries](../architecture/ADR-007-agent-runtime-data-boundaries.md)
- [ADR-008 — platform bounded contexts, deployment freeze, and conformance](../architecture/ADR-008-bounded-contexts-no-new-services.md)

ADR-006/007 contained Python-loop rollback and ownership language that no longer matched the
completed Rust cutover. [`ADR-008`](../architecture/ADR-008-bounded-contexts-no-new-services.md)
(Accepted 2026-08-29) is the successor/conformance record: it preserves the single-kernel,
lease/proof and append-only invariants while superseding those obsolete clauses item by item.
