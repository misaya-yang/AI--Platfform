/**
 * Agent Loop — the core engine that orchestrates LLM ↔ tool execution.
 *
 * Flow:
 * 1. Send user message + OS tool definitions to gateway via SSE
 * 2. Gateway's LLM responds with text and/or tool_calls
 * 3. If tool_call is an OS tool → execute locally, send result back
 * 4. If tool_call is remote → gateway handles it internally
 * 5. Repeat until LLM produces final text response (no more tool_calls)
 *
 * The gateway's agent loop already handles remote tools internally.
 * The CLI only needs to intercept and execute OS-level tools locally.
 */

import type { StreamEvent } from "../types/events.js";
import { EventType } from "../types/events.js";
import type { GatewayClient } from "../remote/client.js";
import type { ChatOptions } from "../remote/chat.js";
import {
  executeTool,
  getOSToolSchemas,
  type ConfirmCallback,
  type PendingToolCall,
} from "./tool-executor.js";

export interface AgentLoopOptions extends ChatOptions {
  /** Callback to request user confirmation for write/bash operations */
  onConfirm?: ConfirmCallback;
  /** Callback for each SSE event (for UI rendering) */
  onEvent?: (event: StreamEvent) => void;
  /** Max iterations to prevent infinite loops */
  maxIterations?: number;
}

interface ToolCallAccumulator {
  callId: string;
  name: string;
  arguments: string; // JSON string being accumulated
}

/**
 * Run the agent loop: send message, handle tool calls, iterate.
 * Returns the final assistant text response.
 */
export async function runAgentLoop(
  client: GatewayClient,
  message: string,
  options: AgentLoopOptions = {},
): Promise<string> {
  const {
    onConfirm,
    onEvent,
    maxIterations = 10,
    ...chatOpts
  } = options;

  const osToolSchemas = getOSToolSchemas();
  let sessionId = chatOpts.sessionId;
  let fullText = "";
  let iteration = 0;

  // The message to send (first iteration = user message, subsequent = tool results)
  let currentMessage = message;
  let toolResults: Array<{ call_id: string; name: string; result: string }> | undefined;

  while (iteration < maxIterations) {
    iteration++;
    let pendingToolCalls: ToolCallAccumulator[] = [];
    let iterationText = "";

    // Build request body
    const body: Record<string, any> = {
      message: currentMessage,
      stream: true,
      model_id: chatOpts.modelId ?? "qwen3.6-plus",
      temperature: chatOpts.temperature ?? 0.7,
      os_agent_enabled: true,
      os_tools: osToolSchemas,
    };

    if (sessionId) body.session_id = sessionId;
    if (chatOpts.kbDatasetIds?.length) body.kb_dataset_ids = chatOpts.kbDatasetIds;
    if (chatOpts.webSearchEnabled) body.web_search_enabled = true;
    if (chatOpts.maxTokens) body.max_tokens = chatOpts.maxTokens;
    if (chatOpts.systemPrompt) body.system_prompt = chatOpts.systemPrompt;
    if (toolResults) body.tool_results = toolResults;

    // Stream SSE
    for await (const event of client.streamSSE(
      "/api/v1/assistant/chat/stream",
      body,
    )) {
      onEvent?.(event);

      switch (event.eventType) {
        case EventType.SESSION_CREATED:
          sessionId = event.data.session_id;
          break;

        case EventType.TEXT_DELTA:
          iterationText += event.data.content ?? event.data.text ?? "";
          break;

        case EventType.TOOL_CALL_START: {
          pendingToolCalls.push({
            callId: event.data.call_id ?? event.data.tool_call_id ?? `call_${pendingToolCalls.length}`,
            name: event.data.tool_name ?? event.data.name ?? "",
            arguments: "",
          });
          break;
        }

        case EventType.TOOL_CALL_ARGS: {
          const last = pendingToolCalls[pendingToolCalls.length - 1];
          if (last) {
            last.arguments += event.data.args ?? event.data.arguments ?? "";
          }
          break;
        }

        case EventType.TOOL_CALL_END: {
          // Tool call is complete; will be processed after stream
          break;
        }

        case EventType.ERROR:
          fullText += `\nError: ${event.data.error ?? event.data.message ?? "Unknown"}`;
          return fullText;
      }
    }

    fullText += iterationText;

    // No tool calls — we're done
    if (pendingToolCalls.length === 0) {
      return fullText;
    }

    // Filter to only OS tools that we handle locally
    const osToolCalls = pendingToolCalls.filter((tc) => {
      // Check if it's in our OS tool list
      return getOSToolSchemas().some((s) => s.function.name === tc.name);
    });

    if (osToolCalls.length === 0) {
      // All tools are remote — gateway handles them. We just return what we got.
      return fullText;
    }

    // Execute OS tools locally
    const results: Array<{ call_id: string; name: string; result: string }> = [];

    for (const tc of osToolCalls) {
      let parsedArgs: Record<string, any> = {};
      try {
        parsedArgs = JSON.parse(tc.arguments || "{}");
      } catch {
        // Try to use as-is
      }

      const execution = await executeTool(
        { callId: tc.callId, name: tc.name, arguments: parsedArgs },
        onConfirm,
      );

      // Emit tool result event for UI
      onEvent?.({
        eventType: EventType.TOOL_CALL_RESULT,
        data: {
          call_id: tc.callId,
          tool_name: tc.name,
          result: execution.result.slice(0, 500),
          status: execution.status,
          duration_ms: execution.durationMs,
        },
        timestamp: Date.now() / 1000,
      });

      results.push({
        call_id: tc.callId,
        name: tc.name,
        result: execution.result,
      });
    }

    // Prepare for next iteration — send tool results back
    currentMessage = "";
    toolResults = results;
  }

  fullText += "\n(Max iterations reached)";
  return fullText;
}
