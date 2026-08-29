// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import assert from "node:assert/strict";
// @ts-expect-error -- node built-ins are supplied by the node --test runtime.
import { test } from "node:test";

import { parseSseEventPart } from "./sseEventParser.ts";

test("SSE parser retains event IDs and joins multiline data", () => {
  assert.deepEqual(
    parseSseEventPart<{ ok: boolean }>(
      'id: dataset-a:8\nevent: terminal\ndata: {"ok":\ndata: true}',
    ),
    { id: "dataset-a:8", event: "terminal", data: { ok: true } },
  );
});

test("SSE parser ignores comments and DONE frames", () => {
  assert.equal(parseSseEventPart(": keep-alive"), null);
  assert.equal(parseSseEventPart("data: [DONE]"), null);
  assert.deepEqual(parseSseEventPart("data: plain"), {
    event: "",
    data: "plain",
  });
});
