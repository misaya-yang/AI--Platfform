/**
 * Runtime resolver for the eval dataset linked to a KB dataset.
 *
 * Kept apart from `evalCaseStore.ts` on purpose: the store is pure mapping /
 * diff / hashing logic (unit-tested under `node --test`, which cannot resolve
 * the `@/` alias at runtime), while this module carries the only API import.
 */

import {
  createEvalDataset,
  getAgentTraceDetail,
  getLatestKbFailureExample,
  listEvalDatasets,
  saveKbFailureRevision,
  type EvalDataset,
} from "@/api/eval";
import {
  findKbEvalDatasetPaged,
  HIT_TEST_EVAL_SOURCE,
  kbEvalDatasetName,
  knowledgeFailureToImportItem,
  KB_EVAL_DATASET_LIST_LIMIT,
  KB_EVAL_DATASET_SOURCE,
  QA_EVAL_SOURCE,
  sourceVersionsFromHits,
  type KnowledgeFailureHit,
} from "./evalCaseStore";

/**
 * Resolve the eval dataset linked to a KB dataset, creating it on first use.
 * Creation is lazy (save/import path) so a workbench that only reads never
 * writes to the eval store.
 */
export async function resolveKbEvalDataset(kbDatasetId: string): Promise<EvalDataset> {
  const found = await findKbEvalDatasetPaged(kbDatasetId, (offset) =>
    listEvalDatasets({ limit: KB_EVAL_DATASET_LIST_LIMIT, offset })
  );
  if (found) return found;
  return createEvalDataset({
    name: kbEvalDatasetName(kbDatasetId),
    description: `Retrieval evaluation cases linked to knowledge dataset ${kbDatasetId}`,
    metadata: { kb_dataset_id: kbDatasetId, source: KB_EVAL_DATASET_SOURCE },
  });
}

/**
 * Save a failed KB observation to the platform Eval review queue. The observed
 * hits are evidence, not labels; the user supplies the expected answer. A
 * repeated save of one trace is deduped by case_id in the authoritative store.
 */
export async function saveKnowledgeFailureToEvalDataset(params: {
  kbDatasetId: string;
  query: string;
  expectedAnswer: string;
  observedHits: KnowledgeFailureHit[];
  source: typeof HIT_TEST_EVAL_SOURCE | typeof QA_EVAL_SOURCE;
  sourceTraceId?: string;
  queryFingerprint?: string;
  observedAnswer?: string;
  failureReason?: string;
}): Promise<{ created: boolean; revision: number }> {
  if (!params.query.trim() || !params.expectedAnswer.trim()) {
    throw new Error("Question and expected answer are required");
  }
  const evalDataset = await resolveKbEvalDataset(params.kbDatasetId);
  let confirmedTraceId: string | undefined;
  if (params.sourceTraceId) {
    for (const delayMs of [0, 100, 300]) {
      if (delayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, delayMs));
      }
      try {
        await getAgentTraceDetail(params.sourceTraceId, "rag");
        confirmedTraceId = params.sourceTraceId;
        break;
      } catch {
        // Trace ingest is asynchronous; retain its ID in metadata for review.
      }
    }
  }
  const item = knowledgeFailureToImportItem({ ...params, confirmedTraceId });
  const latest = await getLatestKbFailureExample(evalDataset.dataset_id, item.case_id);
  const rawRevision = latest?.metadata?.case_revision;
  const expectedRevision = latest
    ? typeof rawRevision === "number" && Number.isInteger(rawRevision) && rawRevision > 0
      ? rawRevision
      : 1
    : 0;
  const response = await saveKbFailureRevision(evalDataset.dataset_id, {
    case_id: item.case_id,
    kb_dataset_id: params.kbDatasetId,
    query: params.query.trim(),
    expected_answer: params.expectedAnswer.trim(),
    source: params.source,
    observed_segment_ids: params.observedHits.map((hit) => hit.segment_id),
    source_versions: sourceVersionsFromHits(params.kbDatasetId, params.observedHits),
    source_trace_id: confirmedTraceId || null,
    kb_trace_id: params.sourceTraceId || null,
    query_fingerprint: params.queryFingerprint || null,
    observed_answer: params.observedAnswer || null,
    failure_reason: params.failureReason?.trim() || null,
    expected_revision: expectedRevision,
  });
  return { created: response.created, revision: response.revision };
}
