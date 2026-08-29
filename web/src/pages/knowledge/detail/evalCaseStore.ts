/**
 * Persistence bridge between the retrieval eval workbench and the platform
 * eval-dataset store (PRD §5-#22, F6).
 *
 * Every KB dataset gets one linked eval dataset, found by
 * `metadata.kb_dataset_id` (created lazily on first save/import). Workbench
 * cases round-trip as eval examples:
 *
 *   input.query                     <- the test question
 *   expected_output.relevant_segment_ids <- the labelled segments
 *   metadata.case_id                <- stable id the store dedupes by
 *
 * The store's `skip_duplicates` import dedupes on `metadata.case_id` alone
 * (verified against the backend repository), so edited cases must go through
 * PATCH while new cases go through import — `diffEvalCases` computes that
 * split. There is no delete endpoint (dependency D7), so cases removed from
 * the workbench stay in the eval dataset; callers report that honestly.
 */

import type { EvalDataset, EvalExample, EvalExampleImportItem } from "@/api/eval";

export const KB_EVAL_DATASET_SOURCE = "kb-retrieval-workbench";
export const HIT_TEST_EVAL_SOURCE = "kb-hit-test";
export const KB_EVAL_SPLIT = "regression";
export const KB_EVAL_DATASET_LIST_LIMIT = 200;
export const KB_EVAL_EXAMPLE_LIST_LIMIT = 500;

export interface PersistedEvalCase {
  caseId: string;
  exampleId: string;
  query: string;
  relevantSegmentIds: string[];
}

export function kbEvalDatasetName(kbDatasetId: string): string {
  return `kb-retrieval-eval-${kbDatasetId}`;
}

export function findKbEvalDataset(
  datasets: EvalDataset[],
  kbDatasetId: string
): EvalDataset | undefined {
  return datasets.find((dataset) => dataset.metadata?.kb_dataset_id === kbDatasetId);
}

/** Normalize `expected_output.relevant_segment_ids`: strings only, trimmed, deduped, order kept. */
export function extractRelevantSegmentIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const ids = value
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .map((item) => item.trim());
  return [...new Set(ids)];
}

function metadataCaseId(example: EvalExample): string {
  const raw = example.metadata?.case_id;
  return typeof raw === "string" && raw.trim().length > 0 ? raw.trim() : "";
}

/**
 * Project an eval example into a workbench case. Only examples carrying a
 * non-empty `input.query` materialize — golden sets written by other surfaces
 * (QA references, trace-derived cases) stay in the eval dataset untouched.
 */
export function exampleToEvalCase(example: EvalExample): PersistedEvalCase | null {
  const rawQuery = example.input?.query;
  const query = typeof rawQuery === "string" ? rawQuery.trim() : "";
  if (!query) return null;
  return {
    caseId: metadataCaseId(example) || example.example_id,
    exampleId: example.example_id,
    query,
    relevantSegmentIds: extractRelevantSegmentIds(example.expected_output?.relevant_segment_ids),
  };
}

/** Build the import item for a workbench case (shape passes backend `validate_case`). */
export function evalCaseToImportItem(params: {
  caseId: string;
  kbDatasetId: string;
  query: string;
  relevantSegmentIds: string[];
  source?: string;
}): EvalExampleImportItem {
  return {
    case_id: params.caseId,
    split: KB_EVAL_SPLIT,
    input: { query: params.query },
    expected_output: { relevant_segment_ids: [...params.relevantSegmentIds] },
    expected_trajectory: {},
    assertions: [],
    metadata: {
      source: params.source ?? KB_EVAL_DATASET_SOURCE,
      kb_dataset_id: params.kbDatasetId,
    },
  };
}

export interface EvalCaseDiff {
  /** Cases whose case_id the store has not seen: send through import. */
  toImport: EvalExampleImportItem[];
  /** Cases whose case_id exists but whose content changed: send through PATCH. */
  toUpdate: Array<{
    exampleId: string;
    caseId: string;
    query: string;
    relevantSegmentIds: string[];
  }>;
  unchangedCount: number;
  /** Saved cases no longer present in the workbench (no delete endpoint — D7). */
  removedFromWorkbenchCount: number;
}

function caseContentKey(query: string, relevantSegmentIds: string[]): string {
  return JSON.stringify({ query: query.trim(), ids: [...relevantSegmentIds].sort() });
}

export function diffEvalCases(params: {
  localCases: Array<{ id: string; query: string; relevantSegmentIds: string[] }>;
  serverCases: PersistedEvalCase[];
  kbDatasetId: string;
}): EvalCaseDiff {
  const serverByCaseId = new Map(params.serverCases.map((entry) => [entry.caseId, entry]));
  const localCaseIds = new Set<string>();
  const diff: EvalCaseDiff = {
    toImport: [],
    toUpdate: [],
    unchangedCount: 0,
    removedFromWorkbenchCount: 0,
  };

  for (const localCase of params.localCases) {
    const serverCase = serverByCaseId.get(localCase.id);
    localCaseIds.add(localCase.id);
    const ids = extractRelevantSegmentIds(localCase.relevantSegmentIds);
    if (!serverCase) {
      diff.toImport.push(
        evalCaseToImportItem({
          caseId: localCase.id,
          kbDatasetId: params.kbDatasetId,
          query: localCase.query,
          relevantSegmentIds: ids,
        })
      );
      continue;
    }
    if (
      caseContentKey(localCase.query, ids) ===
      caseContentKey(serverCase.query, serverCase.relevantSegmentIds)
    ) {
      diff.unchangedCount += 1;
      continue;
    }
    diff.toUpdate.push({
      exampleId: serverCase.exampleId,
      caseId: localCase.id,
      query: localCase.query,
      relevantSegmentIds: ids,
    });
  }

  for (const serverCase of params.serverCases) {
    if (!localCaseIds.has(serverCase.caseId)) diff.removedFromWorkbenchCount += 1;
  }
  return diff;
}

/**
 * FNV-1a 32-bit over the normalized query. Deterministic case ids make
 * repeated "send to eval set" clicks dedupe through `skip_duplicates`
 * instead of piling up copies.
 */
export function hashEvalCaseInput(query: string): string {
  const normalized = query.trim().replace(/\s+/g, " ");
  let hash = 0x811c9dc5;
  for (let index = 0; index < normalized.length; index += 1) {
    hash ^= normalized.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

export function hitTestEvalCaseId(kbDatasetId: string, query: string): string {
  return `kb-hit-${kbDatasetId}-${hashEvalCaseInput(query)}`;
}
