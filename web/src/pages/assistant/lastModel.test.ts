import assert from "node:assert/strict";
import { test } from "node:test";

import { readLastModelId, writeLastModelId } from "./lastModel.ts";

class MemoryStorage {
  private items = new Map<string, string>();

  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }
}

function installStorage(storage: MemoryStorage): void {
  (globalThis as { window?: unknown }).window = {
    localStorage: storage,
  };
}

test("last model cache round-trips through storage", () => {
  installStorage(new MemoryStorage());

  assert.equal(readLastModelId(), "");

  writeLastModelId("qwen3.7-plus");
  assert.equal(readLastModelId(), "qwen3.7-plus");

  writeLastModelId("gpt-4.1");
  assert.equal(readLastModelId(), "gpt-4.1");
});

test("last model cache refuses to write empty ids", () => {
  const storage = new MemoryStorage();
  installStorage(storage);

  writeLastModelId("qwen3.7-plus");
  writeLastModelId("");
  assert.equal(readLastModelId(), "qwen3.7-plus");
});

test("last model cache degrades to empty when storage throws", () => {
  (globalThis as { window?: unknown }).window = {
    get localStorage() {
      throw new Error("storage unavailable");
    },
  };

  assert.equal(readLastModelId(), "");
  writeLastModelId("qwen3.7-plus"); // must not throw
});
