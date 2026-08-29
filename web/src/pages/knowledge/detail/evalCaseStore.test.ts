import assert from "node:assert/strict";
import { test } from "node:test";
import type { EvalExample } from "@/api/eval";
import {
  diffEvalCases,
  evalCaseToImportItem,
  exampleToEvalCase,
  extractRelevantSegmentIds,
  findKbEvalDataset,
  hashEvalCaseInput,
  hitTestEvalCaseId,
  KB_EVAL_DATASET_SOURCE,
  KB_EVAL_SPLIT,
  kbEvalDatasetName,
} from "./evalCaseStore.ts";

function makeExample(overrides: Partial<EvalExample> = {}): EvalExample {
  return {
    example_id: "ex-1",
    dataset_id: "eval-ds-1",
    tenant_id: "tenant-1",
    split: "regression",
    input: { query: "员工手册在哪里？" },
    expected_output: { relevant_segment_ids: ["seg-a", "seg-b"] },
    metadata: { case_id: "case_1" },
    created_by: "user-1",
    ...overrides,
  };
}

test("extractRelevantSegmentIds keeps strings only, trims, dedupes, preserves order", () => {
  assert.deepEqual(
    extractRelevantSegmentIds(["seg-b", " seg-a ", "seg-b", "", "  ", 7, null, { id: "x" }]),
    ["seg-b", "seg-a"]
  );
});

test("extractRelevantSegmentIds returns [] for non-array input", () => {
  assert.deepEqual(extractRelevantSegmentIds(undefined), []);
  assert.deepEqual(extractRelevantSegmentIds("seg-a"), []);
  assert.deepEqual(extractRelevantSegmentIds({ ids: ["seg-a"] }), []);
});

test("exampleToEvalCase projects an example into a workbench case", () => {
  const result = exampleToEvalCase(makeExample());
  assert.deepEqual(result, {
    caseId: "case_1",
    exampleId: "ex-1",
    query: "员工手册在哪里？",
    relevantSegmentIds: ["seg-a", "seg-b"],
  });
});

test("exampleToEvalCase falls back to example_id when metadata.case_id is absent", () => {
  const result = exampleToEvalCase(makeExample({ metadata: {} }));
  assert.equal(result?.caseId, "ex-1");
});

test("exampleToEvalCase returns null without a usable input.query", () => {
  assert.equal(exampleToEvalCase(makeExample({ input: {} })), null);
  assert.equal(exampleToEvalCase(makeExample({ input: { query: "   " } })), null);
  assert.equal(exampleToEvalCase(makeExample({ input: { query: 42 } })), null);
});

test("exampleToEvalCase trims the query", () => {
  const result = exampleToEvalCase(makeExample({ input: { query: "  报销流程  " } }));
  assert.equal(result?.query, "报销流程");
});

test("evalCaseToImportItem carries every field validate_case requires", () => {
  const item = evalCaseToImportItem({
    caseId: "case_1",
    kbDatasetId: "kb-1",
    query: "员工手册在哪里？",
    relevantSegmentIds: ["seg-a", "seg-b"],
  });
  // All six validate_case-required fields are present (backend returns 422
  // when any of case_id/input/expected_output/expected_trajectory/assertions/
  // metadata is missing).
  assert.equal(item.case_id, "case_1");
  assert.deepEqual(item.input, { query: "员工手册在哪里？" });
  assert.deepEqual(item.expected_output, { relevant_segment_ids: ["seg-a", "seg-b"] });
  assert.deepEqual(item.expected_trajectory, {});
  assert.deepEqual(item.assertions, []);
  assert.deepEqual(item.metadata, {
    source: KB_EVAL_DATASET_SOURCE,
    kb_dataset_id: "kb-1",
  });
  assert.equal(item.split, KB_EVAL_SPLIT);
});

test("evalCaseToImportItem honours a custom source", () => {
  const item = evalCaseToImportItem({
    caseId: "case_1",
    kbDatasetId: "kb-1",
    query: "q",
    relevantSegmentIds: [],
    source: "kb-hit-test",
  });
  assert.equal(item.metadata?.source, "kb-hit-test");
});

test("diffEvalCases splits new / changed / unchanged / removed", () => {
  const serverCases = [
    { caseId: "case_keep", exampleId: "ex-keep", query: "保持不变的用例", relevantSegmentIds: ["seg-1"] },
    { caseId: "case_edit", exampleId: "ex-edit", query: "将被修改的用例", relevantSegmentIds: ["seg-2"] },
    { caseId: "case_gone", exampleId: "ex-gone", query: "工作台里已删除", relevantSegmentIds: ["seg-3"] },
  ];
  const localCases = [
    { id: "case_keep", query: "保持不变的用例", relevantSegmentIds: ["seg-1"] },
    { id: "case_edit", query: "将被修改的用例（改）", relevantSegmentIds: ["seg-2"] },
    { id: "case_new", query: "新增用例", relevantSegmentIds: ["seg-9"] },
  ];

  const diff = diffEvalCases({ localCases, serverCases, kbDatasetId: "kb-1" });

  assert.equal(diff.toImport.length, 1);
  assert.equal(diff.toImport[0].case_id, "case_new");
  assert.equal(diff.toImport[0].metadata?.kb_dataset_id, "kb-1");

  assert.equal(diff.toUpdate.length, 1);
  assert.deepEqual(diff.toUpdate[0], {
    exampleId: "ex-edit",
    caseId: "case_edit",
    query: "将被修改的用例（改）",
    relevantSegmentIds: ["seg-2"],
  });

  assert.equal(diff.unchangedCount, 1);
  assert.equal(diff.removedFromWorkbenchCount, 1);
});

test("diffEvalCases treats reordered segment ids and surrounding whitespace as unchanged", () => {
  const serverCases = [
    { caseId: "case_a", exampleId: "ex-a", query: "查询", relevantSegmentIds: ["seg-2", "seg-1"] },
  ];
  const diff = diffEvalCases({
    localCases: [{ id: "case_a", query: "  查询 ", relevantSegmentIds: ["seg-1", "seg-2"] }],
    serverCases,
    kbDatasetId: "kb-1",
  });
  assert.equal(diff.unchangedCount, 1);
  assert.equal(diff.toImport.length, 0);
  assert.equal(diff.toUpdate.length, 0);
});

test("diffEvalCases detects changed segment ids with the same query", () => {
  const serverCases = [
    { caseId: "case_a", exampleId: "ex-a", query: "查询", relevantSegmentIds: ["seg-1"] },
  ];
  const diff = diffEvalCases({
    localCases: [{ id: "case_a", query: "查询", relevantSegmentIds: ["seg-1", "seg-2"] }],
    serverCases,
    kbDatasetId: "kb-1",
  });
  assert.equal(diff.toUpdate.length, 1);
  assert.deepEqual(diff.toUpdate[0].relevantSegmentIds, ["seg-1", "seg-2"]);
});

test("hashEvalCaseInput is deterministic and whitespace-insensitive", () => {
  assert.equal(hashEvalCaseInput("报销 流程 是什么"), hashEvalCaseInput("  报销   流程 是什么  "));
  assert.match(hashEvalCaseInput("任意查询"), /^[0-9a-f]{8}$/);
  assert.notEqual(hashEvalCaseInput("查询一"), hashEvalCaseInput("查询二"));
});

test("hitTestEvalCaseId is stable across repeated sends", () => {
  const first = hitTestEvalCaseId("kb-1", "年假 怎么申请");
  const second = hitTestEvalCaseId("kb-1", "  年假   怎么申请 ");
  assert.equal(first, second);
  assert.equal(first.startsWith("kb-hit-kb-1-"), true);
  assert.notEqual(first, hitTestEvalCaseId("kb-2", "年假怎么申请"));
});

test("findKbEvalDataset matches by metadata.kb_dataset_id only", () => {
  const linked = {
    dataset_id: "eval-linked",
    metadata: { kb_dataset_id: "kb-1" },
  };
  const unrelated = {
    dataset_id: "eval-other",
    metadata: { kb_dataset_id: "kb-2" },
  };
  const bare = { dataset_id: "eval-bare", metadata: {} };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const datasets = [unrelated, bare, linked] as any;
  assert.equal(findKbEvalDataset(datasets, "kb-1")?.dataset_id, "eval-linked");
  assert.equal(findKbEvalDataset(datasets, "kb-missing"), undefined);
});

test("kbEvalDatasetName is deterministic per KB dataset", () => {
  assert.equal(kbEvalDatasetName("kb-1"), "kb-retrieval-eval-kb-1");
});
