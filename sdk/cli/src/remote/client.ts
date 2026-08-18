/**
 * HTTP transport client for the AI Gateway.
 *
 * Uses Node.js native https/http modules for SSE streaming (more reliable
 * than built-in fetch for streaming in Node.js).
 */

import { randomUUID } from "node:crypto";
import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";
import type { CLIConfig } from "../types/config.js";
import type { StreamEvent } from "../types/events.js";
import { EventType } from "../types/events.js";

const CLI_VERSION = "1.0.3";

export class GatewayClient {
  private config: CLIConfig;

  constructor(config: CLIConfig) {
    this.config = config;
  }

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-API-Key": this.config.api_key,
      "X-Tenant-Id": this.config.tenant_id,
      "X-SDK-Version": `cli/${CLI_VERSION}`,
      "X-Request-Id": randomUUID(),
    };
    if (this.config.user_id) {
      headers["X-User-Id"] = this.config.user_id;
    }
    return headers;
  }

  private baseUrl(): string {
    return this.config.base_url.replace(/\/$/, "");
  }

  /**
   * Standard JSON request with auth.
   */
  async request(
    method: string,
    path: string,
    body?: Record<string, any>,
  ): Promise<any> {
    const url = `${this.baseUrl()}${path}`;
    const headers = this.getHeaders();

    const resp = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (resp.ok) return resp.json();

    const errBody = await resp.text();
    throw new Error(`HTTP ${resp.status}: ${errBody || resp.statusText}`);
  }

  /**
   * SSE streaming POST using Node.js native http/https modules.
   * Yields parsed StreamEvent objects.
   */
  async *streamSSE(
    path: string,
    body: Record<string, any>,
  ): AsyncGenerator<StreamEvent> {
    const url = new URL(path, this.baseUrl());
    const headers = this.getHeaders();
    headers["Accept"] = "text/event-stream";

    const payload = JSON.stringify(body);
    headers["Content-Length"] = String(Buffer.byteLength(payload));

    const reqFn = url.protocol === "https:" ? httpsRequest : httpRequest;

    // Create a promise-based wrapper around the native request
    const response = await new Promise<import("node:http").IncomingMessage>(
      (resolve, reject) => {
        const req = reqFn(
          url,
          { method: "POST", headers },
          (res) => {
            if (res.statusCode && res.statusCode >= 400) {
              let body = "";
              res.on("data", (chunk) => (body += chunk));
              res.on("end", () =>
                reject(new Error(`SSE HTTP ${res.statusCode}: ${body || res.statusMessage}`)),
              );
              return;
            }
            resolve(res);
          },
        );
        req.on("error", reject);
        req.write(payload);
        req.end();
      },
    );

    // Parse SSE from the response stream with cleanup
    let buffer = "";
    let bufEvent: string | null = null;
    let bufDataLines: string[] = [];

    try {
      for await (const chunk of response) {
        buffer += typeof chunk === "string" ? chunk : chunk.toString("utf-8");
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const rawLine of lines) {
          const line = rawLine.replace(/\r$/, "");

          if (!line) {
            if (bufDataLines.length > 0) {
              const event = parseSSEEvent(bufEvent, bufDataLines);
              if (event) {
                yield event;
                if (isTerminalEvent(event)) return;
              }
            }
            bufEvent = null;
            bufDataLines = [];
            continue;
          }

          if (line.startsWith("data:")) {
            bufDataLines.push(line[5] === " " ? line.slice(6) : line.slice(5));
          } else if (line.startsWith("event:")) {
            bufEvent = line.slice(6).trim();
          }
        }
      }

      // Flush trailing
      if (bufDataLines.length > 0) {
        const event = parseSSEEvent(bufEvent, bufDataLines);
        if (event) yield event;
      }
    } finally {
      // Ensure stream is destroyed on generator abort or error
      response.destroy();
    }
  }

}

export function isTerminalEvent(event: StreamEvent): boolean {
  return (
    event.eventType === EventType.DONE ||
    event.eventType === EventType.RUN_FINISHED ||
    event.eventType === EventType.ERROR ||
    event.eventType === EventType.RUN_ERROR ||
    event.eventType === EventType.CANCELLED
  );
}

export function parseSSEEvent(
  eventField: string | null,
  dataLines: string[],
): StreamEvent | null {
  const raw = dataLines.join("\n");

  if (raw.trim() === "[DONE]") {
    return { eventType: EventType.DONE, data: {}, timestamp: Date.now() / 1000 };
  }

  let payload: Record<string, any>;
  try {
    payload = JSON.parse(raw);
  } catch {
    payload = { raw };
  }

  const eventType =
    eventField ?? payload.event_type ?? payload.event ?? "message";
  delete payload.event_type;
  delete payload.event;

  const timestamp = payload.timestamp ?? Date.now() / 1000;
  delete payload.timestamp;

  // Unwrap nested "data" — can be string (text_delta) or object
  const inner = payload.data !== undefined ? payload.data : payload;

  let data: Record<string, any>;
  if (typeof inner === "string") {
    data = { content: inner };
  } else if (typeof inner === "object" && inner !== null && !Array.isArray(inner)) {
    data = inner;
  } else {
    data = { value: inner };
  }

  return { eventType, data, timestamp: Number(timestamp) };
}
