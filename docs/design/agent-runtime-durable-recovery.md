# Agent Runtime durable execution and restart recovery

Status: local implementation and required acceptance verified, 2026-09-26. This extends ADR-006/007 and the
R1 J03 contract; it does not introduce another agent loop or migrate platform
governance to Rust. Evidence: [local acceptance](../../reports/product/runtime-durable-recovery-2026-09-26.md), R1 runbook and DR-01 receipt.

## Authority and recovery sequence

1. Rust acquires a renewable PostgreSQL run claim before Core admission. The
   claim has an instance ID and monotonically increasing fence. Every model
   call, tool dispatch, rollout write and terminal projection verifies that
   fence. Losing a claim stops the local turn; a stale owner cannot advance it.
2. Runtime persists the admitted execution context and each dynamic invocation
   before waiting or dispatching. Context pins the original run/turn, snapshot,
   model, tool policy and platform identity. It contains no provider credential.
3. Approvals are keyed by the original run/call/action. Restart reuses the
   original approval ID, deadline and durable decision. A process waiter is a
   notification mechanism, not decision authority. No decision is changed to
   cancelled merely because its old waiter disappeared.
4. Recovery reconciles the original Worker execution before dispatching. A
   terminal result is reused; a live execution is observed; a reserved but
   undispatched execution may continue under valid authority. Dispatched write
   or unknown actions with no trustworthy receipt remain unknown and are never
   replayed. Runtime must not issue a new lease/attempt for an existing execution.
5. The full original tool response is durably paired with its original Core
   call before cold resume. Result injection is idempotent. This precedes Core
   history normalization so an unfinished call is not silently replaced by an
   aborted result. Unsupported native/descendant recovery fails closed.
6. A private in-process host bridge invokes Core `recover_turn_if_idle` with
   the original turn ID, recovered tool outputs and signed model metadata. It
   does not submit user input or use public `turn/start` to simulate recovery.
7. Gateway revalidates the original signed model lease for the current claim,
   without changing the pinned model, provider revision, limits or usage counters.
   Expired or revoked original authority stops recovery rather than extending consent.
   Recovery generations distinguish model attempts after transport/process loss;
   they never authorize replay of a tool action or reset budget consumption.

## Persistence and concurrency

Additive migration adds Runtime execution ownership/context and invocation
journal records. The existing ThreadStore, approval table and Worker execution
ledger remain authoritative. Claim/renew/takeover, approval deduplication and
Worker dispatch checks are transactional. A takeover cannot use an unexpired
claim. Cancelled/terminal runs and revoked/expired snapshots cannot be reclaimed.
Old terminal runs are not resurrected. Historical runs without durable admission
context are reported as nonrecoverable; no guessed model or authorization is used.

All phases preserve tenant/user/session binding. Worker dispatch atomically
rechecks run state, snapshot revocation, model lease and Runtime fence together
with consuming approval. Heartbeats cannot renew an expired or superseded fence.
The current product deployment remains one Runtime instance; the ownership
contract is tested with competing executors, without claiming multi-instance SSE
affinity or full platform high availability.

Worker reservation and cancellation carry the Runtime owner/fence and hold the
original run/owner locks through their mutation transaction. Cancellation reads
the latest execution under a row lock before classifying an already-dispatched
write as unknown. Dispatch and revocation use the same run-first lock order.
Lease checks use wall-clock time after lock acquisition.

SIGINT/SIGTERM stops admission and local capability waiters, then invokes the
existing Core suspension API through a private host command. The Core marker
suppresses fabricated user-cancelled tool output during suspension. Pending
approvals, decisions and Worker receipts stay durable; shutdown does not send a
Worker cancel. The owner remains fenced until its lease expires, so a replacement
process cannot overlap with shutdown. Failure to suspend does not release ownership.
The empty V2 `:recover` endpoint only wakes reconciliation of the original turn;
it cannot change the model, input, authority or execution identity.

## Failure and cancellation semantics

Pending approvals retain their original expiry. Rejection produces a single
denial response and zero Worker dispatches. Cancellation prevents new work and
reconciles already-dispatched actions; it never claims that external effects
were rolled back. Revocation blocks lease renewal, new model calls and dispatch.
Transient recovery failures retain recoverable state and diagnostics; unsupported
or unprovable action recovery emits an explicit unknown/nonrecoverable outcome.
Model transport recovery may incur another model request under the original
budget, but persisted tool calls/results cannot produce a second action.

## Verification contract

The required matrix covers persistence before approval, decision before waiter
notification, Worker reserve before dispatch, dispatch before completion, atomic
side effect/receipt before terminal, Worker terminal before Runtime result,
Runtime result before Core continuation, and continuation before recovery HTTP
response. Also cover concurrent recovery, stale owner, cancellation, revocation,
approval expiry/parameter mismatch and unknown writes. Each receipt separates
isolated automated tests, controlled crash injection and real Qwen/Worker/browser
observations. Rust commands never run on the host; candidate compilation and
scoped verification use the existing Docker builders, with hosted CI evidence
reported separately when available.

Rollback is old-binary-compatible for quiescent history; active newly durable
runs require the recovery-aware Runtime to finish or be safely cancelled before
rolling back. Existing additive tables and immutable receipts are retained.


## Final projection repairs

A recovered Core started event has the same turn identity; history uses one
original start anchor and only a different turn ends its message interval.
This preserves partial body and avoids duplicating fallback and committed Core
body. Different turns with identical prose remain distinct.

Unknown results are immutable tool lifecycle facts. Runtime publishes the
existing safe side_effect_unknown event even when reusing a journal response.
Historical projection also reads original scoped lifecycle-only unknown
receipts, so older records retain their body, diagnostic run ID and uncertainty.
Cold cancellation appends its terminal receipt in the same scoped transaction;
duplicate stop repairs a missing receipt without advancing or replaying a run.

## 2026-09-27：Gateway 当前来源权益校验

Rust保留原run/turn/模型/lease与工具回执，Gateway在新轮、显式恢复与每个恢复模型请求前核验当前账号/来源权益。原线程含有前文知识时，本轮关闭知识库不移除Core上下文；累积来源从原owner的持久snapshot解析，权限复用KB可见目录。撤权或权限无法核验时拒绝推进，不重建run，不自动执行未知动作。

历史、SSE、待审批预览、Quiz与认证下载使用同一来源规则。只读thread响应可携带restricted_source_run_ids，客户端轮询只遮蔽内容并保留公开终态；不是执行所有权或恢复引擎。匿名Quiz必须有可核验且无私有知识的原run来源，不能从dataset_ids为空推断可公开。证据见R1-RP01收据与2026-09-27收尾报告。
