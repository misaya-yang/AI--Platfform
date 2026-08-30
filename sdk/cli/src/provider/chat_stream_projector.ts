import { randomUUID } from "node:crypto";
import type { ServerResponse } from "node:http";

import { CompatibilityError, record } from "./chat_request_adapter.js";
import { readSsePayloads } from "./sse_reader.js";

interface ToolCallState {
  id: string;
  name: string;
  arguments: string;
  outputIndex: number;
  announced: boolean;
}

export async function projectChatStream(
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
