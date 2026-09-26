import assert from "node:assert/strict";
import { test } from "node:test";

import { activeAssistantModels, preferredAssistantModelId } from "./modelSelection.ts";
import type { ModelInfo } from "../../api/assistant.ts";

const models = [
  { id: "claude", provider: "anthropic" },
  { id: "qwen3.7-plus", provider: "dashscope" },
  { id: "qwen3.8-flash", provider: "dashscope" },
] as ModelInfo[];

test("assistant offers only models from configured providers", () => {
  assert.deepEqual(activeAssistantModels(models, ["dashscope"]).map((model) => model.id), [
    "qwen3.7-plus", "qwen3.8-flash",
  ]);
  assert.deepEqual(activeAssistantModels(models, []), []);
});

test("Qwen 3.8 Flash is the new default while explicit available choices survive", () => {
  const active = activeAssistantModels(models, ["dashscope"]);
  assert.equal(preferredAssistantModelId(active, "qwen3.7-plus"), "qwen3.8-flash");
  assert.equal(preferredAssistantModelId(active, "qwen3.7-plus", "qwen3.7-plus"), "qwen3.7-plus");
  assert.equal(preferredAssistantModelId(active, "qwen3.7-plus", "claude"), "qwen3.8-flash");
  assert.equal(preferredAssistantModelId([], "qwen3.8-flash"), "");
});
