// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import assert from "node:assert/strict";
// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import { test } from "node:test";

import type { DocumentBatchOperation } from "@/api/knowledge";
import { partitionIds, summarizeDocumentBatches } from "./batchOperations.ts";


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
