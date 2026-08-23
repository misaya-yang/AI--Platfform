import assert from "node:assert/strict";
import test from "node:test";

import { buildAgentTurnPayload } from "./agentTurnPayload.ts";

test("V2 turn payload forwards ChatRequest controls to the explicit schema", () => {
  const payload = buildAgentTurnPayload("hello", "model-a", "auto", {
    temperature: 0.2,
    execution_profile: "safe",
    memory_mode: "auto",
    system_prompt: "style",
    os_agent_enabled: false,
    local_node_device_id: "node-a",
    local_node_grant_ids: ["grant-a"],
    resume_run_id: "run-a",
    resume_approval_id: "approval-a",
  });

  assert.equal(payload.temperature, 0.2);
  assert.equal(payload.execution_profile, "safe");
  assert.equal(payload.memory_mode, "auto");
  assert.equal(payload.system_prompt, "style");
  assert.equal(payload.os_agent_enabled, false);
  assert.deepEqual(payload.local_node_grant_ids, ["grant-a"]);
  assert.equal(payload.resume_run_id, "run-a");
  assert.equal(payload.resume_approval_id, "approval-a");
});
