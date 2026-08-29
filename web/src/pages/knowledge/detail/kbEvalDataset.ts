/**
 * Runtime resolver for the eval dataset linked to a KB dataset.
 *
 * Kept apart from `evalCaseStore.ts` on purpose: the store is pure mapping /
 * diff / hashing logic (unit-tested under `node --test`, which cannot resolve
 * the `@/` alias at runtime), while this module carries the only API import.
 */

import {
  createEvalDataset,
  importEvalExamples,
  listEvalDatasets,
  type EvalDataset,
} from "@/api/eval";
import {
  evalCaseToImportItem,
  findKbEvalDataset,
  HIT_TEST_EVAL_SOURCE,
  hitTestEvalCaseId,
  kbEvalDatasetName,
  KB_EVAL_DATASET_LIST_LIMIT,
  KB_EVAL_DATASET_SOURCE,
} from "./evalCaseStore";

/**
 * Resolve the eval dataset linked to a KB dataset, creating it on first use.
 * Creation is lazy (save/import path) so a workbench that only reads never
 * writes to the eval store.
 */
export async function resolveKbEvalDataset(kbDatasetId: string): Promise<EvalDataset> {
  const listed = await listEvalDatasets({ limit: KB_EVAL_DATASET_LIST_LIMIT });
  const found = findKbEvalDataset(listed.datasets, kbDatasetId);
  if (found) return found;
  return createEvalDataset({
    name: kbEvalDatasetName(kbDatasetId),
    description: `Retrieval evaluation cases linked to knowledge dataset ${kbDatasetId}`,
    metadata: { kb_dataset_id: kbDatasetId, source: KB_EVAL_DATASET_SOURCE },
  });
}

/**
 * One-click "send to eval set" (PRD §5-#23): persist a retrieval result as a
 * golden case — input=query, expected_output=relevant segments.
 *
 * The case_id is a deterministic hash of the query, so repeated sends of the
 * same query dedupe through `skip_duplicates` instead of piling up copies. The
 * hit-test console and QA tab share the id space: for one query the first
 * send wins and later sends report as skipped.
 *
 * `createEvalExampleFromTrace` is deliberately not used: KB hit-test/QA
 * responses do not expose a trace_id yet, so there is nothing to link. Switch
 * to the trace-derived path once the backend surfaces it.
 */
export async function sendRetrievalCaseToEvalDataset(params: {
  kbDatasetId: string;
  query: string;
  relevantSegmentIds: string[];
  source?: string;
}): Promise<{ imported: number; skipped: number }> {
  const query = params.query.trim();
  if (!query) throw new Error("Cannot send an empty query to the eval set");
  const evalDataset = await resolveKbEvalDataset(params.kbDatasetId);
  const item = evalCaseToImportItem({
    caseId: hitTestEvalCaseId(params.kbDatasetId, query),
    kbDatasetId: params.kbDatasetId,
    query,
    relevantSegmentIds: params.relevantSegmentIds,
    source: params.source ?? HIT_TEST_EVAL_SOURCE,
  });
  const response = await importEvalExamples(evalDataset.dataset_id, [item], {
    mode: "skip_duplicates",
  });
  return { imported: response.imported, skipped: response.skipped };
}
