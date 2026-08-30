import assert from "node:assert/strict";
import test from "node:test";

import {
  buildDatasetUploadConfigPatch,
  uploadDatasetFiles,
} from "./datasetUploadModel.ts";

interface TestFile {
  name: string;
}

test("existing documents keep immutable ingestion identity during upload", () => {
  assert.deepEqual(
    buildDatasetUploadConfigPatch({
      documentCount: 3,
      chunkingConfig: { mode: "automatic" },
      rerank: { enabled: true, model: "gte-rerank" },
      embedding: {
        changed: true,
        provider: "dashscope",
        model: "text-embedding-v4",
        dimension: 1024,
      },
    }),
    { retrieval_config: { rerank: { enabled: true, model: "gte-rerank" } } }
  );
});

test("empty datasets may establish chunking and embedding identity", () => {
  assert.deepEqual(
    buildDatasetUploadConfigPatch({
      documentCount: 0,
      chunkingConfig: { mode: "heading" },
      rerank: { enabled: false, model: "gte-rerank" },
      embedding: {
        changed: true,
        provider: "dashscope",
        model: "text-embedding-v4",
        dimension: 1024,
      },
    }),
    {
      chunking_config: { mode: "heading" },
      retrieval_config: { rerank: { enabled: false, model: "gte-rerank" } },
      embedding_provider: "dashscope",
      embedding_model: "text-embedding-v4",
      embedding_dimension: 1024,
    }
  );
});

test("single uploads retain only failed files for retry", async () => {
  const files: TestFile[] = [{ name: "accepted.pdf" }, { name: "retry.pdf" }];
  const outcome = await uploadDatasetFiles(files, {
    uploadBatch: async () => ({ accepted: 0, errors: [] }),
    uploadOne: async (file) => {
      if (file.name === "retry.pdf") throw new Error("provider unavailable");
    },
    describeError: (error) => (error as Error).message,
  });

  assert.equal(outcome.accepted, 1);
  assert.deepEqual(outcome.failures, [
    { file: files[1], error: "provider unavailable" },
  ]);
});

test("batch uploads map server rejections back to retryable files", async () => {
  const files: TestFile[] = [
    { name: "one.pdf" },
    { name: "two.html" },
    { name: "three.docx" },
  ];
  const outcome = await uploadDatasetFiles(files, {
    uploadBatch: async () => ({
      accepted: 2,
      errors: [{ filename: "two.html", error: "parser limit" }],
    }),
    uploadOne: async () => undefined,
    describeError: () => "unused",
  });

  assert.equal(outcome.accepted, 2);
  assert.deepEqual(outcome.failures, [
    { file: files[1], error: "parser limit" },
  ]);
});

test("batch receipt mismatch fails closed instead of losing a source", async () => {
  await assert.rejects(
    uploadDatasetFiles(
      [{ name: "one.pdf" }, { name: "two.pdf" }, { name: "three.pdf" }],
      {
        uploadBatch: async () => ({
          accepted: 2,
          errors: [{ filename: "not-submitted.pdf", error: "rejected" }],
        }),
        uploadOne: async () => undefined,
        describeError: () => "unused",
      }
    ),
    /do not match/
  );
});

test("batch receipt count mismatch also fails closed", async () => {
  await assert.rejects(
    uploadDatasetFiles(
      [{ name: "one.pdf" }, { name: "two.pdf" }, { name: "three.pdf" }],
      {
        uploadBatch: async () => ({ accepted: 1, errors: [] }),
        uploadOne: async () => undefined,
        describeError: () => "unused",
      }
    ),
    /do not match/
  );
});
