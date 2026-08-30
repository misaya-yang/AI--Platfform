export interface BatchUploadError {
  filename: string;
  error: string;
}

interface BatchUploadReceipt {
  accepted: number;
  errors: BatchUploadError[];
}

interface UploadHandlers<TFile extends { name: string }> {
  uploadBatch: (files: TFile[]) => Promise<BatchUploadReceipt>;
  uploadOne: (file: TFile) => Promise<unknown>;
  describeError: (error: unknown) => string;
}

export interface DatasetUploadOutcome<TFile> {
  accepted: number;
  failures: Array<{ file: TFile; error: string }>;
}

/** Upload once, retaining every rejected source so the same dialog can retry it. */
export async function uploadDatasetFiles<TFile extends { name: string }>(
  files: TFile[],
  handlers: UploadHandlers<TFile>
): Promise<DatasetUploadOutcome<TFile>> {
  if (files.length >= 3) {
    const receipt = await handlers.uploadBatch(files);
    const errorsByName = new Map(
      receipt.errors.map((failure) => [failure.filename, failure.error])
    );
    const failures = files
      .filter((file) => errorsByName.has(file.name))
      .map((file) => ({ file, error: errorsByName.get(file.name)! }));

    if (
      failures.length !== receipt.errors.length ||
      receipt.accepted + receipt.errors.length !== files.length
    ) {
      throw new Error("Batch upload returned errors that do not match the submitted files");
    }
    return { accepted: receipt.accepted, failures };
  }

  const failures: Array<{ file: TFile; error: string }> = [];
  for (const file of files) {
    try {
      await handlers.uploadOne(file);
    } catch (error) {
      failures.push({ file, error: handlers.describeError(error) });
    }
  }
  return { accepted: files.length - failures.length, failures };
}
