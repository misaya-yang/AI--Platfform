# Phase 06 - Give the index plane capacity

- PHASE_ID: PPR-06
- FEATURE_ID: PPR-F007
- DEPENDS_ON: PPR-05

## Outcome

Ingestion throughput scales without touching interactive retrieval latency, and any CPU kernel that moves to Rust proves byte-identical output first.

## Starting position (verified 2026-08-26)

Ingestion and retrieval are **already process-separated**: `KNOWLEDGE_RUNTIME_ROLE` supports `all|api|worker`; `knowledge-service` runs as `api` with a `DurableEnqueueProxy` and `knowledge-worker` runs as `worker`. **Do not re-do this split.** What remains is capacity and CPU cost.

## Scope

In:

- Horizontal scale for `knowledge-worker` (today `--workers 1`), with claim/lease safety under N workers.
- An interactive retrieval profile: short recall for the agent tool path, wide recall plus rerank reserved for eval/accurate presets.
- **Conditional:** native kernels (`chunk` / `tokenize` / `hash` / `dedupe`) as a Rust crate with PyO3 bindings — only if measurement shows CPU is the binding constraint.

Out:

- Retrieval quality changes: RRF weights, rerank policy, recall width defaults belong to `kb-rag-optimization-plan.md`.
- Reimplementing BM25 or sparse retrieval (already native in PostgreSQL tsvector and Qdrant).
- A separate ingestion service. The process split already exists.

## Done when

- [ ] `knowledge-worker` runs N > 1 without double-claiming a document; a concurrent claim fuzz proves it.
- [ ] Ingesting a 200-page PDF raises interactive retrieval p99 by ≤ 10%.
- [ ] Ingestion throughput is ≥ 3× the PPR-00 baseline, or the report states which resource is the real ceiling.
- [ ] If native kernels ship: chunk boundaries are **byte-identical** to the Python implementation over the corpus; if they are not, the kernel work is cancelled and that is recorded.
- [ ] Interactive retrieval profile is measurable and does not change eval-path recall.
- [ ] Full regression passes.

## Verify

| Check | Command or observation | Proves |
| --- | --- | --- |
| Claim safety | N-worker concurrent claim fuzz | No double processing |
| Interference | 200-page ingest during retrieval load | p99 delta ≤ 10% |
| Chunk equivalence | Corpus-wide boundary diff, Python vs Rust | Byte-identical or cancelled |
| Quality unchanged | `make rag-eval-regression-gate` | Recall/quality untouched |

## Stop or confirm

- **Cancel the kernel work rather than accept a chunk-boundary delta.** A drift forces a full re-embed of every dataset.
- Stop if horizontal scale requires changing the durable queue's ownership semantics.
