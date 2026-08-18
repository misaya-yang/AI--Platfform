import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { isTerminalEvent, parseSSEEvent } from "./client.js";

const fixture = JSON.parse(
  readFileSync(
    resolve(process.cwd(), "../fixtures/sse_inner_envelopes.json"),
    "utf-8",
  ),
);

function parseFixture(name: string) {
  const dataLine = String(fixture[name].sse)
    .split("\n")
    .find((line) => line.startsWith("data:"));
  if (!dataLine) throw new Error(`missing data line for ${name}`);
  return parseSSEEvent(null, [dataLine.slice(5).trimStart()]);
}

describe("shared SSE inner-envelope fixture", () => {
  it("unwraps text_delta data strings", () => {
    const event = parseFixture("text_delta");
    expect(event?.eventType).toBe("text_delta");
    expect(event?.data.content).toBe("Hi");
  });

  it.each(["done", "error", "cancelled", "run_finished", "run_error"])(
    "recognizes %s as terminal",
    (name) => {
      const event = parseFixture(name);
      expect(event?.eventType).toBe(fixture[name].event_type);
      expect(event && isTerminalEvent(event)).toBe(true);
      if (name === "error") expect(event?.data.message).toBe("boom");
      if (name === "cancelled") expect(event?.data.reason).toBe("user_stop");
      if (name === "run_error") expect(event?.data.message).toBe("run failed");
    },
  );

  it.each(["null_data", "number_data", "boolean_data", "array_data"])(
    "wraps %s in value",
    (name) => {
      const event = parseFixture(name);
      expect(event?.data).toEqual({ value: fixture[name].value });
    },
  );
});
