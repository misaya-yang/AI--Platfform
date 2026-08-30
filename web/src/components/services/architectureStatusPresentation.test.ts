import assert from "node:assert/strict";
import test from "node:test";

import {
  architectureStatusClass,
  architectureStatusTone,
  canViewArchitectureStatus,
} from "./architectureStatusPresentation.ts";

test("architecture status presentation separates degraded and lifecycle states", () => {
  assert.equal(architectureStatusTone("healthy"), "healthy");
  assert.equal(architectureStatusTone("unavailable"), "degraded");
  assert.equal(architectureStatusTone("one-shot"), "inactive");
  assert.equal(architectureStatusTone("unexpected"), "unknown");
  assert.match(architectureStatusClass("degraded"), /amber/);
});

test("architecture status visibility matches the platform-admin backend contract", () => {
  assert.equal(canViewArchitectureStatus(["platform_admin"], []), true);
  assert.equal(canViewArchitectureStatus([], ["super_admin"]), true);
  assert.equal(canViewArchitectureStatus(["admin"], ["console:services:view"]), false);
});
