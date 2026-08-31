export interface ResponsesRequest {
  model: string;
  input: unknown;
  instructions?: unknown;
  tools?: unknown;
  tool_choice?: unknown;
  parallel_tool_calls?: unknown;
  temperature?: unknown;
  max_output_tokens?: unknown;
  reasoning?: unknown;
  stream?: unknown;
  store?: unknown;
  stream_options?: unknown;
  include?: unknown;
  service_tier?: unknown;
  prompt_cache_key?: unknown;
  text?: unknown;
  client_metadata?: unknown;
  access_programs?: unknown;
}

const SUPPORTED_REQUEST_FIELDS = new Set([
  "model", "input", "instructions", "tools", "tool_choice",
  "parallel_tool_calls", "temperature", "max_output_tokens", "reasoning",
  "stream", "store", "stream_options", "include", "service_tier",
  "prompt_cache_key", "text", "client_metadata", "access_programs",
]);

/** Convert the lossless Responses subset accepted by a Chat-only provider. */
export function responsesToChat(body: ResponsesRequest): Record<string, unknown> {
  validateRequestEnvelope(body);
  if (typeof body.model !== "string" || !body.model.trim()) {
    throw new CompatibilityError("responses_model_required");
  }
  const messages = responsesInputToMessages(body.input, body.instructions);
  const chat: Record<string, unknown> = {
    model: body.model,
    messages,
    stream: true,
    stream_options: { include_usage: true },
  };
  if (body.temperature !== undefined) chat.temperature = finiteNumber(body.temperature, "responses_temperature_invalid");
  if (body.max_output_tokens !== undefined) chat.max_tokens = positiveInteger(body.max_output_tokens, "responses_max_tokens_invalid");
  if (body.reasoning !== undefined) {
    const reasoning = record(body.reasoning, "responses_reasoning_unsupported");
    if (reasoning.effort !== undefined) chat.reasoning_effort = text(reasoning.effort, "responses_reasoning_unsupported");
  }
  if (body.service_tier !== undefined && body.service_tier !== null) {
    chat.service_tier = text(body.service_tier, "responses_service_tier_invalid");
  }
  if (body.prompt_cache_key !== undefined && body.prompt_cache_key !== null) {
    chat.prompt_cache_key = text(body.prompt_cache_key, "responses_prompt_cache_key_invalid");
  }
  if (body.tools !== undefined) {
    if (!Array.isArray(body.tools)) throw new CompatibilityError("responses_tools_invalid");
    const projectedTools: Array<Record<string, unknown>> = [];
    const projectedNames = new Set<string>();
    const appendFunction = (tool: unknown) => {
      const value = record(tool, "responses_tool_unsupported");
      const functionValue = value.function && typeof value.function === "object"
        ? record(value.function, "responses_tool_invalid") : value;
      const name = text(functionValue.name, "responses_tool_invalid");
      if (projectedNames.has(name)) throw new CompatibilityError("responses_tool_duplicate");
      projectedNames.add(name);
      projectedTools.push({
        type: "function",
        function: {
          name,
          description: typeof functionValue.description === "string" ? functionValue.description : "",
          parameters: functionValue.parameters ?? { type: "object", properties: {} },
          ...(typeof functionValue.strict === "boolean" ? { strict: functionValue.strict } : {}),
        },
      });
    };
    for (const tool of body.tools) {
      const value = record(tool, "responses_tool_unsupported");
      if (value.type === "function") appendFunction(value);
      else if (value.type === "namespace") {
        if (!Array.isArray(value.tools)) throw new CompatibilityError("responses_tool_unsupported");
        for (const child of value.tools) {
          const childValue = record(child, "responses_tool_unsupported");
          if (childValue.type !== "function") throw new CompatibilityError("responses_tool_unsupported");
          appendFunction(childValue);
        }
      } else throw new CompatibilityError("responses_tool_unsupported");
    }
    const toolChoice = chatToolChoice(body.tool_choice);
    if (!projectedTools.length && !["auto", "none"].includes(String(toolChoice))) {
      throw new CompatibilityError("responses_tool_choice_requires_tools");
    }
    if (projectedTools.length) {
      chat.tools = projectedTools;
      chat.tool_choice = toolChoice;
    }
    if (projectedTools.length && body.parallel_tool_calls !== undefined) {
      if (typeof body.parallel_tool_calls !== "boolean") {
        throw new CompatibilityError("responses_parallel_tool_calls_invalid");
      }
      chat.parallel_tool_calls = body.parallel_tool_calls;
    }
  } else if (body.tool_choice !== undefined && !["auto", "none"].includes(String(body.tool_choice))) {
    throw new CompatibilityError("responses_tool_choice_requires_tools");
  }
  return chat;
}

function validateRequestEnvelope(body: ResponsesRequest): void {
  const raw = body as unknown as Record<string, unknown>;
  const unknown = Object.keys(raw).filter((key) => !SUPPORTED_REQUEST_FIELDS.has(key));
  if (unknown.length) throw new CompatibilityError("responses_fields_unsupported");
  if (body.stream !== true) throw new CompatibilityError("streaming_responses_required");
  if (body.store !== undefined && body.store !== false) throw new CompatibilityError("responses_store_unsupported");
  if (body.stream_options !== undefined && body.stream_options !== null) {
    const options = record(body.stream_options, "responses_stream_options_unsupported");
    if (Object.keys(options).length) throw new CompatibilityError("responses_stream_options_unsupported");
  }
  if (body.include !== undefined) {
    if (!Array.isArray(body.include) || body.include.some((value) => value !== "reasoning.encrypted_content")) {
      throw new CompatibilityError("responses_include_unsupported");
    }
  }
  if (body.text !== undefined && body.text !== null) throw new CompatibilityError("responses_text_controls_unsupported");
  if (body.access_programs !== undefined && body.access_programs !== null) {
    throw new CompatibilityError("responses_access_programs_unsupported");
  }
  if (body.client_metadata !== undefined && body.client_metadata !== null) {
    record(body.client_metadata, "responses_client_metadata_invalid");
  }
}

function responsesInputToMessages(input: unknown, instructions: unknown): Array<Record<string, unknown>> {
  const messages: Array<Record<string, unknown>> = [];
  if (instructions !== undefined) messages.push({ role: "system", content: text(instructions, "responses_instructions_invalid") });
  if (typeof input === "string") return [...messages, { role: "user", content: input }];
  if (!Array.isArray(input)) throw new CompatibilityError("responses_input_invalid");
  const pendingCalls = new Map<string, string>();
  for (const raw of input) {
    const item = record(raw, "responses_input_item_unsupported");
    const type = item.type ?? "message";
    if (type === "message") {
      const role = item.role;
      if (!["user", "assistant", "system", "developer"].includes(String(role))) {
        throw new CompatibilityError("responses_message_role_unsupported");
      }
      messages.push({
        role: role === "developer" ? "system" : role,
        content: responseContentText(item.content),
      });
      continue;
    }
    if (type === "agent_message" || type === "reasoning") {
      throw new CompatibilityError("responses_input_item_unsupported");
    }
    if (type === "function_call") {
      const id = text(item.call_id ?? item.id, "responses_function_call_invalid");
      const name = text(item.name, "responses_function_call_invalid");
      const args = typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments ?? {});
      if (pendingCalls.has(id)) throw new CompatibilityError("responses_function_call_duplicate");
      pendingCalls.set(id, name);
      messages.push({ role: "assistant", content: "", tool_calls: [{ id, type: "function", function: { name, arguments: args } }] });
      continue;
    }
    if (type === "function_call_output") {
      const id = text(item.call_id, "responses_function_output_invalid");
      const name = pendingCalls.get(id);
      if (!name) throw new CompatibilityError("responses_function_output_unmatched");
      messages.push({ role: "tool", tool_call_id: id, name, content: functionOutputText(item.output) });
      pendingCalls.delete(id);
      continue;
    }
    throw new CompatibilityError("responses_input_item_unsupported");
  }
  if (pendingCalls.size) throw new CompatibilityError("responses_function_output_missing");
  if (!messages.length) throw new CompatibilityError("responses_input_empty");
  return messages;
}

function functionOutputText(output: unknown): string {
  if (typeof output === "string") return output;
  if (!Array.isArray(output)) throw new CompatibilityError("responses_function_output_unsupported");
  return output.map((part) => {
    const value = record(part, "responses_function_output_unsupported");
    if (value.type !== "input_text") throw new CompatibilityError("responses_function_output_unsupported");
    return text(value.text, "responses_function_output_unsupported");
  }).join("");
}

function responseContentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) throw new CompatibilityError("responses_content_unsupported");
  return content.map((part) => {
    const value = record(part, "responses_content_unsupported");
    if (!["input_text", "output_text", "text"].includes(String(value.type))) {
      throw new CompatibilityError("responses_content_unsupported");
    }
    return text(value.text, "responses_content_unsupported");
  }).join("");
}

function chatToolChoice(choice: unknown): unknown {
  if (choice === undefined || choice === null) return "auto";
  if (["auto", "none", "required"].includes(String(choice))) return choice;
  const value = record(choice, "responses_tool_choice_unsupported");
  if (value.type !== "function") throw new CompatibilityError("responses_tool_choice_unsupported");
  const functionValue = value.function && typeof value.function === "object"
    ? record(value.function, "responses_tool_choice_unsupported") : value;
  return { type: "function", function: { name: text(functionValue.name, "responses_tool_choice_unsupported") } };
}

export function record(value: unknown, code: string): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new CompatibilityError(code);
  return value as Record<string, any>;
}

function text(value: unknown, code: string): string {
  if (typeof value !== "string" || !value) throw new CompatibilityError(code);
  return value;
}

function finiteNumber(value: unknown, code: string): number {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new CompatibilityError(code);
  return number;
}

function positiveInteger(value: unknown, code: string): number {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) throw new CompatibilityError(code);
  return number;
}

export class CompatibilityError extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "CompatibilityError";
  }
}
