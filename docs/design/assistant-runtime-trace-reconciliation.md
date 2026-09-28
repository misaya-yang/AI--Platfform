# Durable Assistant Runtime Trace reconciliation

The Gateway writes one Eval Trace for each terminal Agent Runtime turn. The
stream observer can disappear when a browser disconnects or the Gateway
restarts, so a Gateway background reader also reconciles terminal turns from
the durable Runtime tables. It never starts, resumes, or replays a turn.

`assistant_runs` supplies the original run UUID, scope, status, and timestamps.
`assistant_runtime_snapshots` must match that run's tenant, user, session, and
snapshot identity. `assistant_runtime_items` must contain a terminal V1 event
for that same original turn and scope. `completed` and `succeeded` map to Trace
`succeeded`; `failed` and `cancelled` retain their status. A run without a
matching terminal event is skipped until a later scan.

The reader scans at most 100 runs per page inside the Trace retention window,
advancing by terminal timestamp and run UUID. It reads visible `text_delta`
chunks by database cursor, so long turns do not depend on the SSE 1,000-event
page limit. Only the first 2,000 visible text characters feed the existing
bounded redaction helper. Other event types contribute counts only; raw
reasoning and tool payloads are not copied to the Trace. Measured model tokens
and exact micro-USD cost are included only when the durable model-call ledger
has complete receipts for every dispatched call.

The original run UUID is the Trace UUID. The single Trace terminal event uses
the original Runtime event UUID and carries its Runtime sequence. A database
transaction locks the scoped run, inserts the Trace/span/event once, and
creates one `trace.ingested` outbox job. Normal live Runtime Trace ingestion
locks the same Trace row and treats an outbox job in **any** state as already
enqueued. Repeated Gateway restarts therefore leave the same Trace and job.

The original HTTP request ID is not stored in the Runtime ledger, so recovered
Traces use the original run UUID as `request_id`. First-token latency is set to
zero because a cold read cannot reconstruct when the first token was observed.
