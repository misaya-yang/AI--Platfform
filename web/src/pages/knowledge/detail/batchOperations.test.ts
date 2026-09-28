// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import assert from "node:assert/strict";
// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import { test } from "node:test";

import type { DocumentBatchOperation } from "@/api/knowledge";
import { documentBatchItemOutcomes, hasBlockingUnknown, partitionIds, settleDocumentBatchAttempt, summarizeDocumentBatches } from "./batchOperations.ts";


function operation(
  total: number,
  succeeded: number,
  skipped: number,
  failed: number
): DocumentBatchOperation {
  return {
    operation_id: crypto.randomUUID(),
    tenant_id: "tenant-a",
    dataset_id: "dataset-a",
    operation: "reembed",
    status: skipped || failed ? "partial" : "completed",
    total_count: total,
    queued_count: succeeded,
    skipped_count: skipped,
    failed_count: failed,
    problem_items: [],
    problem_items_truncated: false,
  };
}


test("partitionIds never truncates selections beyond two hundred", () => {
  const ids = Array.from({ length: 251 }, (_, index) => `doc-${index}`);
  const chunks = partitionIds(ids, 100);

  assert.deepEqual(chunks.map((chunk) => chunk.length), [100, 100, 51]);
  assert.deepEqual(chunks.flat(), ids);
});


test("partitionIds de-duplicates without changing first-seen order", () => {
  assert.deepEqual(partitionIds(["b", "a", "b", "c"], 2), [["b", "a"], ["c"]]);
});


test("summarizeDocumentBatches preserves partial outcomes", () => {
  assert.deepEqual(
    summarizeDocumentBatches([
      operation(100, 97, 2, 1),
      operation(51, 50, 1, 0),
    ]),
    { total: 151, succeeded: 147, skipped: 3, failed: 1 }
  );
});

test("per-item receipt preserves success, failure, and unknown without guessing", () => {
  const partial = operation(3, 1, 1, 1);
  partial.problem_items = [
    { document_id: "b", status: "skipped" },
    { document_id: "c", status: "failed" },
  ];
  const attempt = { requestedIds: ["a", "b", "c"], operation: partial, outcomeUnknown: false };
  assert.deepEqual(Array.from(documentBatchItemOutcomes(attempt)), [
    ["a", "succeeded"], ["b", "skipped"], ["c", "failed"],
  ]);
  partial.problem_items_truncated = true;
  assert.deepEqual(Array.from(documentBatchItemOutcomes(attempt).values()), ["unknown", "unknown", "unknown"]);
  partial.problem_items_truncated = false;
  assert.deepEqual(Array.from(documentBatchItemOutcomes({ ...attempt, outcomeUnknown: true }).values()), [
    "unknown", "unknown", "unknown",
  ]);
  partial.problem_items[0].document_id = "unexpected";
  assert.deepEqual(Array.from(documentBatchItemOutcomes(attempt).values()), ["unknown", "unknown", "unknown"]);
});

test("POST accepted then poll timeout retries the same operation with GET only", async () => {
  let posts = 0;
  let acceptedCallbacks = 0;
  const accepted = operation(1, 0, 0, 0);
  accepted.status = "running";
  const submit = async () => { posts += 1; return accepted; };
  let polls = 0;
  const wait = async (id: string) => {
    assert.equal(id, accepted.operation_id);
    polls += 1;
    if (polls === 1) throw new Error("poll timeout");
    return { ...operation(1, 1, 0, 0), operation_id: accepted.operation_id };
  };
  const onAccepted = () => { acceptedCallbacks += 1; };
  const first = await settleDocumentBatchAttempt(["doc-1"], undefined, submit, wait, onAccepted);
  assert.equal(first.outcomeUnknown, true);
  assert.equal(first.operation?.operation_id, accepted.operation_id);
  const second = await settleDocumentBatchAttempt(["doc-1"], first, submit, wait, onAccepted);
  assert.equal(second.outcomeUnknown, false);
  assert.equal(posts, 1);
  assert.equal(acceptedCallbacks, 1);
  assert.equal(polls, 2);
});

test("unknown POST without operation ID cannot be blindly resubmitted", async () => {
  let posts = 0;
  const submit = async () => { posts += 1; throw new Error("request timed out"); };
  const wait = async () => operation(1, 1, 0, 0);
  const first = await settleDocumentBatchAttempt(["doc-1"], undefined, submit, wait);
  const second = await settleDocumentBatchAttempt(["doc-1"], first, submit, wait);
  assert.equal(first.outcomeUnknown, true);
  assert.equal(second.outcomeUnknown, true);
  assert.equal(posts, 1);
  assert.equal(hasBlockingUnknown([second]), true);
  assert.equal(hasBlockingUnknown([{ ...second, acknowledged: true }]), false);
});
