import type { DocumentBatchOperation } from "@/api/knowledge";

export interface DocumentBatchSummary {
  total: number;
  succeeded: number;
  skipped: number;
  failed: number;
}

export interface DocumentBatchAttempt {
  requestedIds: string[];
  operation?: DocumentBatchOperation;
  /** A request or receipt poll failed; the server may still be processing it. */
  outcomeUnknown: boolean;
  /** Explicitly released after the operator checked a request with no ID. */
  acknowledged?: boolean;
}

export function hasBlockingUnknown(attempts: readonly DocumentBatchAttempt[]): boolean {
  return attempts.some((attempt) => attempt.outcomeUnknown && !attempt.acknowledged);
}

/** Reconcile a prior ambiguous attempt by operation ID; never submit it again. */
export async function settleDocumentBatchAttempt(
  requestedIds: string[],
  previous: DocumentBatchAttempt | undefined,
  submit: () => Promise<DocumentBatchOperation>,
  wait: (operationId: string) => Promise<DocumentBatchOperation>,
  onAccepted?: (operation: DocumentBatchOperation) => void
): Promise<DocumentBatchAttempt> {
  if (previous && !previous.outcomeUnknown) return previous;
  if (previous && !previous.operation?.operation_id) return previous;
  let operation = previous?.operation;
  try {
    if (!operation) {
      operation = await submit();
      onAccepted?.(operation);
    }
    const resolved = await wait(operation.operation_id);
    if (resolved.operation_id !== operation.operation_id) throw new Error("batch receipt identity mismatch");
    operation = resolved;
    return { requestedIds, operation, outcomeUnknown: false };
  } catch {
    return { requestedIds, operation, outcomeUnknown: true };
  }
}

export type DocumentBatchItemOutcome = "succeeded" | "skipped" | "failed" | "unknown";

/** Only infer per-item success when the terminal receipt accounts for every requested ID. */
export function documentBatchItemOutcomes(
  attempt: DocumentBatchAttempt
): Map<string, DocumentBatchItemOutcome> {
  const outcomes = new Map<string, DocumentBatchItemOutcome>();
  const operation = attempt.operation;
  for (const id of attempt.requestedIds) outcomes.set(id, "unknown");
  if (!operation || attempt.outcomeUnknown || operation.problem_items_truncated) return outcomes;
  const problems = new Map(operation.problem_items.map((item) => [item.document_id, item.status]));
  const reconciled = operation.total_count === attempt.requestedIds.length
    && operation.queued_count + operation.skipped_count + operation.failed_count === operation.total_count
    && problems.size === operation.skipped_count + operation.failed_count
    && [...problems.keys()].every((id) => outcomes.has(id));
  if (!reconciled) return outcomes;
  for (const id of attempt.requestedIds) {
    outcomes.set(id, problems.get(id) ?? "succeeded");
  }
  return outcomes;
}

export function partitionIds(ids: Iterable<string>, size: number): string[][] {
  if (!Number.isSafeInteger(size) || size < 1) throw new Error("batch size must be positive");
  const normalized = Array.from(new Set(Array.from(ids).filter(Boolean)));
  const chunks: string[][] = [];
  for (let offset = 0; offset < normalized.length; offset += size) {
    chunks.push(normalized.slice(offset, offset + size));
  }
  return chunks;
}

export function summarizeDocumentBatches(
  operations: readonly DocumentBatchOperation[]
): DocumentBatchSummary {
  return operations.reduce<DocumentBatchSummary>(
    (summary, operation) => ({
      total: summary.total + operation.total_count,
      succeeded: summary.succeeded + operation.queued_count,
      skipped: summary.skipped + operation.skipped_count,
      failed: summary.failed + operation.failed_count,
    }),
    { total: 0, succeeded: 0, skipped: 0, failed: 0 }
  );
}
