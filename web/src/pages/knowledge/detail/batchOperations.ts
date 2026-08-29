import type { DocumentBatchOperation } from "@/api/knowledge";

export interface DocumentBatchSummary {
  total: number;
  succeeded: number;
  skipped: number;
  failed: number;
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
