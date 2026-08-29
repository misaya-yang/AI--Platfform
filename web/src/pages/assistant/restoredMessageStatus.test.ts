import assert from "node:assert/strict";
import { test } from "node:test";

import { restoredMessageStatus } from "./restoredMessageStatus.ts";

test("restored terminal status preserves cancelled and failed turns", () => {
  assert.equal(restoredMessageStatus("cancelled"), "cancelled");
  assert.equal(restoredMessageStatus("failed"), "failed");
});

test("restored active and successful summaries map to chat turn states", () => {
  assert.equal(restoredMessageStatus("running"), "streaming");
  assert.equal(restoredMessageStatus("blocked"), "streaming");
  assert.equal(restoredMessageStatus("succeeded"), "completed");
  assert.equal(restoredMessageStatus(undefined), "completed");
});
