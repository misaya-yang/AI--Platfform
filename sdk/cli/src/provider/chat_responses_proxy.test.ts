import { createServer } from "node:http";
import { afterEach, describe, expect, it } from "vitest";

import {
  LOCAL_PROXY_TOKEN_ENV,
  responsesToChat,
  startChatCompatibilityProxy,
  type ChatCompatibilityProxy,
} from "./chat_responses_proxy.js";
import type { ProviderProfile } from "./config.js";

const cleanups: Array<() => Promise<void>> = [];
afterEach(async () => {
  while (cleanups.length) await cleanups.pop()!();
});

async function mockChatProvider(
  handler: (request: { url: string; headers: Record<string, string | string[] | undefined>; body: any; attempt: number }) => {
    status?: number;
    chunks?: string[];
    holdOpen?: boolean;
    onClose?: () => void;
  },
) {
  let attempt = 0;
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const result = handler({
      url: request.url ?? "",
      headers: request.headers,
      body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
      attempt: ++attempt,
    });
    response.writeHead(result.status ?? 200, { "Content-Type": "text/event-stream" });
    if (result.holdOpen) response.flushHeaders();
    for (const chunk of result.chunks ?? []) response.write(chunk);
    if (result.holdOpen) response.once("close", () => result.onClose?.());
    else response.end();
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("mock provider did not bind");
  cleanups.push(() => new Promise<void>((resolve) => {
    server.close(() => resolve());
    server.closeAllConnections();
  }));
  return { baseUrl: `http://127.0.0.1:${address.port}/v1`, attempts: () => attempt };
}

function profile(baseUrl: string): ProviderProfile {
  return {
    name: "Chat-only provider",
    model: "chat-model",
    base_url: baseUrl,
    wire_api: "chat_completions",
    auth: { type: "bearer", api_key_env: "CHAT_PROVIDER_KEY" },
    query_params: { "api-version": "2026-08-01" },
    request_max_retries: 1,
    stream_idle_timeout_ms: 2_000,
    allow_insecure_localhost: true,
  };
}

async function startProxy(provider: ProviderProfile): Promise<ChatCompatibilityProxy> {
  const proxy = await startChatCompatibilityProxy(provider, { CHAT_PROVIDER_KEY: "synthetic-secret" });
  cleanups.push(() => proxy.close());
  return proxy;
}

describe("Responses to Chat Completions compatibility", () => {
  it("projects text, reasoning, usage, and tool deltas into Responses SSE", async () => {
    const provider = await mockChatProvider(({ url, headers, body }) => {
      expect(url).toBe("/v1/chat/completions?api-version=2026-08-01");
      expect(headers.authorization).toBe("Bearer synthetic-secret");
      expect(body.model).toBe("chat-model");
      expect(body.messages).toEqual([{ role: "user", content: "hello" }]);
      expect(body.tools[0].function.name).toBe("lookup");
      return { chunks: [
        'data: {"choices":[{"delta":{"reasoning_content":"think","content":"Hi "}}]}\r\n\r\n',
        'data: {"choices":[{"delta":{"content":"there","tool_calls":[{"index":0,"function":{"name":"lookup","arguments":"{\\"q\\":"}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"arguments":"\\"x\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}\n\n',
        "data: [DONE]\n\n",
      ] };
    });
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${proxy.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "chat-model",
        input: "hello",
        stream: true,
        tools: [{ type: "function", name: "lookup", description: "Lookup", parameters: { type: "object" } }],
      }),
    });
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).toContain("event: response.created");
    expect(text).toContain('"delta":"Hi "');
    expect(text).toContain("response.reasoning_summary_text.delta");
    expect(text).toContain('"text":"think"');
    expect(text).toContain('"name":"lookup"');
    expect(text).toContain('"arguments":"{\\"q\\":\\"x\\"}"');
    expect(text).toContain('"input_tokens":3');
    expect(text).toContain("event: response.completed");
    expect(text).not.toContain("synthetic-secret");
    const events = parseSseEvents(text);
    const toolEvents = events.filter((event) => String(event.type).includes("function_call") || event.item?.type === "function_call");
    expect(new Set(toolEvents.map((event) => event.item_id ?? event.item?.call_id))).toEqual(new Set(["call_1"]));
  });

  it("translates tool transcript input for the next agent turn", () => {
    const result = responsesToChat({
      model: "chat-model",
      stream: true,
      instructions: "be concise",
      input: [
        { type: "message", role: "user", content: [{ type: "input_text", text: "find x" }] },
        { type: "function_call", call_id: "call_1", name: "lookup", arguments: "{\"q\":\"x\"}" },
        { type: "function_call_output", call_id: "call_1", output: "found" },
      ],
    });
    expect(result.messages).toEqual([
      { role: "system", content: "be concise" },
      { role: "user", content: "find x" },
      { role: "assistant", content: "", tool_calls: [{ id: "call_1", type: "function", function: { name: "lookup", arguments: "{\"q\":\"x\"}" } }] },
      { role: "tool", tool_call_id: "call_1", name: "lookup", content: "found" },
    ]);

    const contentItems = responsesToChat({
      model: "chat-model",
      stream: true,
      input: [
        { type: "function_call", call_id: "call_2", name: "lookup", arguments: "{}" },
        { type: "function_call_output", call_id: "call_2", output: [
          { type: "input_text", text: "part one" },
          { type: "input_text", text: " and two" },
        ] },
      ],
    });
    expect((contentItems.messages as any[])[1].content).toBe("part one and two");
    expect(() => responsesToChat({
      model: "chat-model",
      stream: true,
      input: [
        { type: "function_call", call_id: "call_3", name: "lookup", arguments: "{}" },
        { type: "function_call_output", call_id: "call_3", output: [
          { type: "input_image", image_url: "data:image/png;base64,eA==" },
        ] },
      ],
    })).toThrow(/responses_function_output_unsupported/);
  });

  it("flattens Runtime namespaces and omits native web search for Chat providers", () => {
    const result = responsesToChat({
      model: "chat-model",
      stream: true,
      input: "hello",
      tools: [
        { type: "web_search" },
        {
          type: "namespace",
          name: "mcp",
          tools: [
            {
              type: "function",
              name: "write",
              description: "Write one value",
              parameters: { type: "object", properties: {} },
            },
          ],
        },
      ],
    });

    expect((result.tools as any[]).map((tool) => tool.function.name)).toEqual(["write"]);
  });

  it("fails closed for image input instead of silently dropping it", () => {
    expect(() => responsesToChat({
      model: "chat-model",
      stream: true,
      input: [{ type: "message", role: "user", content: [{ type: "input_image", image_url: "https://example.test/x.png" }] }],
    })).toThrow(/responses_content_unsupported/);
  });

  it("rejects unknown fields, reasoning history, and non-boolean parallel tools", () => {
    expect(() => responsesToChat({
      model: "chat-model", input: "hello", stream: true, previous_response_id: "resp_1",
    } as any)).toThrow(/responses_fields_unsupported/);
    expect(() => responsesToChat({
      model: "chat-model", stream: true, input: [{ type: "reasoning", summary: [] }],
    })).toThrow(/responses_input_item_unsupported/);
    expect(() => responsesToChat({
      model: "chat-model", stream: true, input: "hello",
      tools: [{ type: "function", name: "lookup" }], parallel_tool_calls: "false",
    })).toThrow(/responses_parallel_tool_calls_invalid/);
  });

  it("retries a pre-stream 429 once and never exposes the provider error body", async () => {
    const provider = await mockChatProvider(({ attempt }) => attempt === 1
      ? { status: 429, chunks: ["provider private diagnostic"] }
      : { chunks: ['data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'] });
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    const text = await response.text();
    expect(provider.attempts()).toBe(2);
    expect(text).toContain('"delta":"ok"');
    expect(text).not.toContain("provider private diagnostic");
  });

  it("rejects callers without the ephemeral loopback token", async () => {
    const provider = await mockChatProvider(() => ({ chunks: [] }));
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    expect(response.status).toBe(401);
    expect(await response.text()).not.toContain(LOCAL_PROXY_TOKEN_ENV);
  });

  it("fails an incomplete provider stream instead of fabricating completion", async () => {
    const provider = await mockChatProvider(() => ({
      chunks: ['data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
    }));
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    const text = await response.text();
    expect(text).toContain('"delta":"partial"');
    expect(text).toContain("provider_stream_incomplete");
    expect(text).toContain("event: response.failed");
    expect(text).not.toContain("event: response.completed");
    const sequences = [...text.matchAll(/"sequence_number":(\d+)/g)].map((match) => Number(match[1]));
    expect(sequences).toEqual(sequences.map((_, index) => index));
  });

  it.each(["length", "content_filter"])("projects finish_reason=%s as failure", async (finishReason) => {
    const provider = await mockChatProvider(() => ({
      chunks: [
        `data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"${finishReason}"}]}\n\n`,
        "data: [DONE]\n\n",
      ],
    }));
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    const text = await response.text();
    expect(text).toContain(`provider_finish_${finishReason}`);
    expect(text).toContain("event: response.failed");
    expect(text).not.toContain("event: response.completed");
  });

  it("cancels the provider stream when the native client disconnects", async () => {
    let providerClosed!: () => void;
    const closed = new Promise<void>((resolve) => { providerClosed = resolve; });
    const provider = await mockChatProvider(() => ({
      chunks: ['data: {"choices":[{"delta":{"content":"first"}}]}\n\n'],
      holdOpen: true,
      onClose: providerClosed,
    }));
    const proxy = await startProxy(profile(provider.baseUrl));
    const controller = new AbortController();
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
      signal: controller.signal,
    });
    await response.body!.getReader().read();
    controller.abort();
    await expect(Promise.race([
      closed.then(() => "closed"),
      new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 750)),
    ])).resolves.toBe("closed");
  });

  it("fails an idle stream and closes the provider connection", async () => {
    let providerClosed!: () => void;
    const closed = new Promise<void>((resolve) => { providerClosed = resolve; });
    const provider = await mockChatProvider(() => ({
      holdOpen: true,
      onClose: providerClosed,
    }));
    const chatProfile = profile(provider.baseUrl);
    chatProfile.stream_idle_timeout_ms = 50;
    const proxy = await startProxy(chatProfile);
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    const text = await response.text();
    expect(text).toContain("provider_stream_idle_timeout");
    expect(text).toContain("event: response.failed");
    expect(text).not.toContain("event: response.completed");
    await expect(Promise.race([
      closed.then(() => "closed"),
      new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 750)),
    ])).resolves.toBe("closed");
  });

  it.each([
    {
      name: "changed tool id",
      chunks: [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_2","function":{"arguments":""}}]},"finish_reason":"tool_calls"}]}\n\n',
        "data: [DONE]\n\n",
      ],
      code: "provider_tool_id_changed",
    },
    {
      name: "missing tool id",
      chunks: [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"lookup","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n',
        "data: [DONE]\n\n",
      ],
      code: "provider_tool_identity_missing",
    },
  ])("fails closed for $name", async ({ chunks, code }) => {
    const provider = await mockChatProvider(() => ({ chunks }));
    const proxy = await startProxy(profile(provider.baseUrl));
    const response = await fetch(`${proxy.baseUrl}/responses`, {
      method: "POST",
      headers: { Authorization: `Bearer ${proxy.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "chat-model", input: "hello", stream: true }),
    });
    const text = await response.text();
    expect(text).toContain(code);
    expect(text).toContain("event: response.failed");
    expect(text).not.toContain("event: response.completed");
  });
});

function parseSseEvents(payload: string): any[] {
  return payload.split(/\r?\n\r?\n/).flatMap((frame) => {
    const data = frame.split(/\r?\n/).find((line) => line.startsWith("data:"));
    return data ? [JSON.parse(data.slice(5).trim())] : [];
  });
}
