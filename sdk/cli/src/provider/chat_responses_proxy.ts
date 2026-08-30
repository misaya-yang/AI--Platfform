import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import type { ProviderProfile } from "./config.js";
import {
  CompatibilityError,
  record,
  responsesToChat,
  type ResponsesRequest,
} from "./chat_request_adapter.js";
import { readSsePayloads } from "./sse_reader.js";

export { responsesToChat } from "./chat_request_adapter.js";

export const LOCAL_PROXY_TOKEN_ENV = "AI_GATEWAY_LOCAL_PROXY_TOKEN";

export interface ChatCompatibilityProxy {
  baseUrl: string;
  token: string;
  close(): Promise<void>;
}

interface ToolCallState {
  id: string;
  name: string;
  arguments: string;
  outputIndex: number;
  announced: boolean;
}

export async function startChatCompatibilityProxy(
  provider: ProviderProfile,
  env: NodeJS.ProcessEnv = process.env,
): Promise<ChatCompatibilityProxy> {
  if (provider.wire_api !== "chat_completions") {
    throw new Error("Chat compatibility proxy requires a chat_completions provider");
  }
  const token = randomBytes(32).toString("base64url");
  const server = createServer((request, response) => {
    void handleProxyRequest(request, response, provider, env, token);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("Local Responses compatibility proxy did not bind a TCP port");
  }
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    token,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    }),
  };
}

async function handleProxyRequest(
  request: IncomingMessage,
  response: ServerResponse,
  provider: ProviderProfile,
  env: NodeJS.ProcessEnv,
  localToken: string,
): Promise<void> {
  response.setHeader("Cache-Control", "no-store");
  response.setHeader("X-Content-Type-Options", "nosniff");
  if (request.method !== "POST" || request.url !== "/v1/responses") {
    jsonError(response, 404, "unsupported_local_proxy_route");
    return;
  }
  if (!matchesLocalToken(request.headers.authorization, localToken)) {
    jsonError(response, 401, "local_proxy_authentication_failed");
    return;
  }

  const clientAbort = new AbortController();
  request.once("aborted", () => clientAbort.abort(new Error("CLI runtime disconnected")));
  response.once("close", () => {
    if (!response.writableEnded) clientAbort.abort(new Error("CLI runtime disconnected"));
  });
  try {
    let body: ResponsesRequest;
    try {
      body = JSON.parse(await readBody(request, 16 * 1024 * 1024)) as ResponsesRequest;
    } catch {
      throw new CompatibilityError("responses_json_invalid");
    }
    if (!body || typeof body !== "object" || body.stream !== true) {
      jsonError(response, 400, "streaming_responses_required");
      return;
    }
    const chatBody = responsesToChat(body);
    const upstream = await openUpstream(provider, env, chatBody, clientAbort.signal);
    if (!upstream.ok) {
      jsonError(response, 502, `provider_http_${upstream.status}`);
      return;
    }
    if (!upstream.body) {
      jsonError(response, 502, "provider_stream_missing");
      return;
    }
    response.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-store",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });
    await projectChatStream(
      upstream,
      response,
      String(chatBody.model),
      provider.stream_idle_timeout_ms ?? 300_000,
      clientAbort.signal,
    );
  } catch (error) {
    if (response.writableEnded || response.destroyed) return;
    const code = error instanceof CompatibilityError ? error.code : "chat_compatibility_stream_failed";
    if (!response.headersSent) jsonError(response, error instanceof CompatibilityError ? 400 : 502, code);
    else response.destroy(new Error(code));
  }
}

async function openUpstream(
  provider: ProviderProfile,
  env: NodeJS.ProcessEnv,
  body: Record<string, unknown>,
  signal: AbortSignal,
): Promise<Response> {
  const url = chatCompletionsUrl(provider);
  const headers = providerHeaders(provider, env);
  const retries = provider.request_max_retries ?? 4;
  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    if (signal.aborted) throw signal.reason;
    const controller = new AbortController();
    const forward = () => controller.abort(signal.reason);
    signal.addEventListener("abort", forward, { once: true });
    const timer = setTimeout(() => controller.abort(new Error("provider connect timeout")), 30_000);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (![429, 500, 502, 503, 504].includes(response.status) || attempt === retries) return response;
      await response.body?.cancel();
      const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
      await delay(Math.min(retryAfter ?? 250 * 2 ** attempt, 5_000), signal);
    } catch (error) {
      lastError = error;
      if (attempt === retries || signal.aborted) throw error;
      await delay(Math.min(250 * 2 ** attempt, 5_000), signal);
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", forward);
    }
  }
  throw lastError ?? new Error("provider request failed");
}

function providerHeaders(provider: ProviderProfile, env: NodeJS.ProcessEnv): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    ...provider.http_headers,
  };
  if (provider.auth.type !== "none") {
    const key = env[provider.auth.api_key_env]?.trim();
    if (!key) throw new CompatibilityError("provider_credential_missing");
    if (provider.auth.type === "bearer") headers.Authorization = `Bearer ${key}`;
    else headers[provider.auth.header] = key;
  }
  for (const [header, envName] of Object.entries(provider.env_http_headers ?? {})) {
    const value = env[envName]?.trim();
    if (!value) throw new CompatibilityError("provider_header_credential_missing");
    headers[header] = value;
  }
  return headers;
}

function chatCompletionsUrl(provider: ProviderProfile): string {
  const url = new URL(provider.base_url);
  const trimmed = url.pathname.replace(/\/$/, "");
  url.pathname = /\/chat\/completions$/i.test(trimmed)
    ? trimmed
    : `${trimmed}/chat/completions`.replace(/\/+/g, "/");
  for (const [key, value] of Object.entries(provider.query_params ?? {})) url.searchParams.set(key, value);
  return url.toString();
}

async function projectChatStream(
  upstream: Response,
  output: ServerResponse,
  model: string,
  idleTimeoutMs: number,
  signal: AbortSignal,
): Promise<void> {
  const responseId = `resp_${randomUUID().replaceAll("-", "")}`;
  const messageId = `msg_${responseId.slice(5)}`;
  const reasoningId = `rs_${responseId.slice(5)}`;
  let sequence = 0;
  let textOutput = "";
  let reasoningOutput = "";
  let messageOutputIndex: number | undefined;
  let reasoningOutputIndex: number | undefined;
  let reasoningClosed = false;
  let nextOutputIndex = 0;
  let usage: Record<string, unknown> | null = null;
  let terminalSeen = false;
  let terminalFailure: string | undefined;
  const tools = new Map<number, ToolCallState>();
  const emit = (type: string, payload: Record<string, unknown>) => {
    writeSse(output, type, { type, sequence_number: sequence++, ...payload });
  };
  const ensureMessageOpen = () => {
    if (messageOutputIndex !== undefined) return;
    messageOutputIndex = nextOutputIndex++;
    emit("response.output_item.added", {
      output_index: messageOutputIndex,
      item: {
        id: messageId,
        type: "message",
        role: "assistant",
        status: "in_progress",
        content: [],
      },
    });
    emit("response.content_part.added", {
      item_id: messageId,
      output_index: messageOutputIndex,
      content_index: 0,
      part: { type: "output_text", text: "", annotations: [] },
    });
  };
  const closeReasoning = () => {
    if (reasoningOutputIndex === undefined || reasoningClosed) return;
    const item = {
      id: reasoningId,
      type: "reasoning",
      status: "completed",
      summary: [{ type: "summary_text", text: reasoningOutput }],
    };
    emit("response.reasoning_summary_text.done", {
      item_id: reasoningId,
      output_index: reasoningOutputIndex,
      summary_index: 0,
      text: reasoningOutput,
    });
    emit("response.reasoning_summary_part.done", {
      item_id: reasoningId,
      output_index: reasoningOutputIndex,
      summary_index: 0,
      part: { type: "summary_text", text: reasoningOutput },
    });
    emit("response.output_item.done", { output_index: reasoningOutputIndex, item });
    reasoningClosed = true;
  };
  const responseObject = (status: string, error: Record<string, unknown> | null = null) => ({
    id: responseId,
    object: "response",
    created_at: Math.floor(Date.now() / 1000),
    status,
    error,
    model,
    output: [],
    usage,
  });
  emit("response.created", { response: responseObject("in_progress") });
  emit("response.in_progress", { response: responseObject("in_progress") });

  try {
    for await (const payload of readSsePayloads(upstream.body!, idleTimeoutMs, signal)) {
    if (payload === "[DONE]") {
      terminalSeen = true;
      break;
    }
    let event: Record<string, unknown>;
    try {
      event = record(JSON.parse(payload), "provider_stream_event_invalid");
    } catch (error) {
      if (error instanceof CompatibilityError) throw error;
      throw new CompatibilityError("provider_stream_json_invalid");
    }
    if (event.usage && typeof event.usage === "object") usage = normalizeUsage(record(event.usage, "provider_usage_invalid"));
    const choices = Array.isArray(event.choices) ? event.choices : [];
    for (const rawChoice of choices) {
      const choice = record(rawChoice, "provider_choice_invalid");
      if (choice.finish_reason !== undefined && choice.finish_reason !== null) {
        const finishReason = String(choice.finish_reason);
        terminalSeen = true;
        if (!["stop", "tool_calls", "function_call"].includes(finishReason)) {
          terminalFailure = `provider_finish_${finishReason.replace(/[^A-Za-z0-9_.-]/g, "_")}`;
        }
      }
      const delta = choice.delta && typeof choice.delta === "object" ? record(choice.delta, "provider_delta_invalid") : {};
      const content = typeof delta.content === "string" ? delta.content : "";
      if (delta.content !== undefined && delta.content !== null && typeof delta.content !== "string") {
        throw new CompatibilityError("provider_content_delta_unsupported");
      }
      if (content) {
        closeReasoning();
        ensureMessageOpen();
        textOutput += content;
        emit("response.output_text.delta", {
          item_id: messageId,
          output_index: messageOutputIndex,
          content_index: 0,
          delta: content,
          logprobs: [],
        });
      }
      const rawReasoning = delta.reasoning_content ?? delta.reasoning;
      if (rawReasoning !== undefined && rawReasoning !== null) {
        if (typeof rawReasoning !== "string") {
          throw new CompatibilityError("provider_reasoning_delta_unsupported");
        }
        if (rawReasoning) {
          if (reasoningOutputIndex === undefined) {
            reasoningOutputIndex = nextOutputIndex++;
            emit("response.output_item.added", {
              output_index: reasoningOutputIndex,
              item: {
                id: reasoningId,
                type: "reasoning",
                status: "in_progress",
                summary: [],
              },
            });
            emit("response.reasoning_summary_part.added", {
              item_id: reasoningId,
              output_index: reasoningOutputIndex,
              summary_index: 0,
              part: { type: "summary_text", text: "" },
            });
          }
          reasoningOutput += rawReasoning;
          emit("response.reasoning_summary_text.delta", {
            item_id: reasoningId,
            output_index: reasoningOutputIndex,
            summary_index: 0,
            delta: rawReasoning,
          });
        }
      }
      if (Array.isArray(delta.tool_calls)) {
        for (const rawTool of delta.tool_calls) {
          const tool = record(rawTool, "provider_tool_delta_invalid");
          const index = Number(tool.index ?? 0);
          if (!Number.isInteger(index) || index < 0) throw new CompatibilityError("provider_tool_delta_invalid");
          const fn = tool.function && typeof tool.function === "object" ? record(tool.function, "provider_tool_delta_invalid") : {};
          const current = tools.get(index) ?? {
            id: "",
            name: "",
            arguments: "",
            outputIndex: nextOutputIndex++,
            announced: false,
          };
          const wasAnnounced = current.announced;
          if (typeof tool.id === "string" && tool.id) {
            if (current.announced && current.id !== tool.id) {
              throw new CompatibilityError("provider_tool_id_changed");
            }
            current.id = tool.id;
          }
          if (typeof fn.name === "string") current.name += fn.name;
          if (typeof fn.arguments === "string") current.arguments += fn.arguments;
          if (!current.announced && current.name && current.id) {
            emit("response.output_item.added", {
              output_index: current.outputIndex,
              item: { id: current.id, type: "function_call", status: "in_progress", call_id: current.id, name: current.name, arguments: "" },
            });
            current.announced = true;
          }
          if (current.announced && current.arguments && !wasAnnounced) {
            emit("response.function_call_arguments.delta", {
              item_id: current.id, output_index: current.outputIndex, delta: current.arguments,
            });
          } else if (current.announced && typeof fn.arguments === "string" && fn.arguments) {
            emit("response.function_call_arguments.delta", {
              item_id: current.id, output_index: current.outputIndex, delta: fn.arguments,
            });
          }
          tools.set(index, current);
        }
      }
    }
  }
  } catch (error) {
    if (signal.aborted || output.destroyed) {
      output.destroy();
      return;
    }
    const code = error instanceof CompatibilityError ? error.code : "provider_stream_failed";
    emit("response.failed", {
      response: {
        ...responseObject("failed", { code, message: code, type: "server_error" }),
        output: [],
      },
    });
    output.end();
    return;
  }
  if (!terminalSeen) {
    emit("response.failed", {
      response: {
        ...responseObject("failed", {
          code: "provider_stream_incomplete",
          message: "provider_stream_incomplete",
          type: "server_error",
        }),
        output: [],
      },
    });
    output.end();
    return;
  }
  if (terminalFailure) {
    emit("response.failed", {
      response: {
        ...responseObject("failed", {
          code: terminalFailure,
          message: terminalFailure,
          type: "server_error",
        }),
        output: [],
      },
    });
    output.end();
    return;
  }
  if ([...tools.values()].some((tool) => !tool.id || !tool.name || !tool.announced)) {
    emit("response.failed", {
      response: {
        ...responseObject("failed", {
          code: "provider_tool_identity_missing",
          message: "provider_tool_identity_missing",
          type: "server_error",
        }),
        output: [],
      },
    });
    output.end();
    return;
  }

  closeReasoning();
  ensureMessageOpen();
  emit("response.output_text.done", {
    item_id: messageId,
    output_index: messageOutputIndex,
    content_index: 0,
    text: textOutput,
    logprobs: [],
  });
  const part = { type: "output_text", text: textOutput, annotations: [] };
  emit("response.content_part.done", {
    item_id: messageId,
    output_index: messageOutputIndex,
    content_index: 0,
    part,
  });
  const messageItem = {
    id: messageId,
    type: "message",
    role: "assistant",
    status: "completed",
    content: [part],
  };
  emit("response.output_item.done", {
    output_index: messageOutputIndex,
    item: messageItem,
  });
  const indexedOutput: Array<{ index: number; item: Record<string, unknown> }> = [
    { index: messageOutputIndex!, item: messageItem },
  ];
  if (reasoningOutputIndex !== undefined) {
    indexedOutput.push({
      index: reasoningOutputIndex,
      item: {
        id: reasoningId,
        type: "reasoning",
        status: "completed",
        summary: [{ type: "summary_text", text: reasoningOutput }],
      },
    });
  }
  for (const tool of [...tools.values()].sort((a, b) => a.outputIndex - b.outputIndex)) {
    emit("response.function_call_arguments.done", {
      item_id: tool.id, output_index: tool.outputIndex, name: tool.name, arguments: tool.arguments,
    });
    const item = { id: tool.id, type: "function_call", status: "completed", call_id: tool.id, name: tool.name, arguments: tool.arguments };
    emit("response.output_item.done", { output_index: tool.outputIndex, item });
    indexedOutput.push({ index: tool.outputIndex, item });
  }
  indexedOutput.sort((a, b) => a.index - b.index);
  const completed = {
    ...responseObject("completed"),
    output: indexedOutput.map(({ item }) => item),
  };
  emit("response.completed", { response: completed });
  output.end();
}

function matchesLocalToken(header: string | undefined, token: string): boolean {
  if (!header) return false;
  const expected = Buffer.from(`Bearer ${token}`);
  const actual = Buffer.from(header);
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

function normalizeUsage(value: Record<string, unknown>): Record<string, unknown> {
  return {
    input_tokens: Number(value.prompt_tokens ?? value.input_tokens ?? 0),
    output_tokens: Number(value.completion_tokens ?? value.output_tokens ?? 0),
    total_tokens: Number(value.total_tokens ?? 0),
  };
}

function writeSse(response: ServerResponse, type: string, payload: Record<string, unknown>): void {
  response.write(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`);
}

function jsonError(response: ServerResponse, status: number, code: string): void {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify({ error: { code, message: code, type: status >= 500 ? "server_error" : "invalid_request_error" } }));
}

async function readBody(request: IncomingMessage, limit: number): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += value.length;
    if (size > limit) throw new CompatibilityError("responses_request_too_large");
    chunks.push(value);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function parseRetryAfter(value: string | null): number | undefined {
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined;
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, ms);
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      reject(signal.reason);
    };
    function done() {
      signal.removeEventListener("abort", abort);
      resolve();
    }
    signal.addEventListener("abort", abort, { once: true });
  });
}
