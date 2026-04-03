/**
 * Ink main application — full agent loop integration.
 * Renders: header, messages, streaming output, tool calls, confirmation prompt, input.
 */

import React, { useState, useCallback, useRef } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import type { CLIConfig } from "./types/config.js";
import { EventType, type StreamEvent } from "./types/events.js";
import { GatewayClient } from "./remote/client.js";
import { ChatModule } from "./remote/chat.js";
import { parseSlashCommand } from "./agent/slash-commands.js";
import { saveMemory, listMemories } from "./memory.js";
import { MCPManager } from "./mcp/manager.js";
import {
  listSkills,
  uploadSkill,
  createAndUploadSkill,
  toggleSkill,
  deleteSkill,
  testSkill,
} from "./skills.js";

// ─── Types ───────────────────────────────────────────────────────────

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  tools?: ToolDisplay[];
  agents?: AgentDisplay[];
}

interface ToolDisplay {
  name: string;
  status: "running" | "done" | "error" | "denied";
  result?: string;
  durationMs?: number;
}

interface AgentDisplay {
  id: string;
  type: "explore" | "task" | "plan" | string;
  description: string;
  status: "running" | "done" | "failed";
  tools: Array<{ name: string; status: string; summary?: string }>;
  streamingText: string;
  resultSummary: string;
  durationMs?: number;
}

interface ConfirmState {
  summary: string;
  toolName: string;
  resolve: (approved: boolean) => void;
}

// ─── Main App ────────────────────────────────────────────────────────

export function App({ config }: { config: CLIConfig }) {
  const { exit } = useApp();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  // Streaming state
  const [streamText, setStreamText] = useState("");
  const [streamTools, setStreamTools] = useState<ToolDisplay[]>([]);
  const [streamAgents, setStreamAgents] = useState<AgentDisplay[]>([]);
  const [statusMsg, setStatusMsg] = useState("");

  // Confirmation prompt
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);

  // Session state
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [model, setModel] = useState(config.default_model);
  const [kbIds, setKbIds] = useState<string[]>(config.kb_dataset_ids ?? []);

  // Refs for accessing latest state in async callbacks
  const streamTextRef = useRef("");
  const streamToolsRef = useRef<ToolDisplay[]>([]);
  const streamAgentsRef = useRef<AgentDisplay[]>([]);

  const client = React.useMemo(() => new GatewayClient(config), [config]);
  const chat = React.useMemo(() => new ChatModule(client), [client]);
  const mcpManager = React.useMemo(() => new MCPManager(), []);

  // ─── Confirmation handler ──────────────────────────────────────

  const handleConfirm = useCallback(
    (summary: string, toolName: string): Promise<boolean> => {
      return new Promise((resolve) => {
        setConfirm({ summary, toolName, resolve });
      });
    },
    [],
  );

  // ─── SSE event handler ────────────────────────────────────────

  const handleEvent = useCallback((event: StreamEvent) => {
    switch (event.eventType) {
      case EventType.SESSION_CREATED:
        setSessionId(event.data.session_id);
        break;

      case EventType.TEXT_DELTA: {
        const text = event.data.content ?? event.data.text ?? "";
        setStreamText((t) => {
          const updated = t + text;
          streamTextRef.current = updated;
          return updated;
        });
        break;
      }

      case EventType.STATUS:
        setStatusMsg(event.data.message ?? event.data.status ?? "");
        break;

      case EventType.TOOL_CALL_START:
        setStreamTools((tools) => {
          const updated = [
            ...tools,
            {
              name: event.data.tool_name ?? event.data.name ?? "unknown",
              status: "running" as const,
            },
          ];
          streamToolsRef.current = updated;
          return updated;
        });
        break;

      case EventType.TOOL_CALL_RESULT:
      case EventType.TOOL_RESULT:
        setStreamTools((tools) => {
          const updated = [...tools];
          const last = updated.findLast((t) => t.status === "running");
          if (last) {
            last.status = (event.data.status as any) ?? "done";
            last.result = truncate(
              typeof event.data.result === "string"
                ? event.data.result
                : JSON.stringify(event.data.result ?? ""),
              200,
            );
            last.durationMs = event.data.duration_ms;
          }
          streamToolsRef.current = updated;
          return updated;
        });
        break;

      case EventType.TOOL_ERROR:
        setStreamTools((tools) => {
          const updated = [...tools];
          const last = updated.findLast((t) => t.status === "running");
          if (last) {
            last.status = "error";
            last.result = event.data.error ?? "Tool error";
          }
          streamToolsRef.current = updated;
          return updated;
        });
        break;

      case EventType.SUBAGENT_STARTED:
        setStreamAgents((agents) => {
          const updated = [
            ...agents,
            {
              id: event.data.agent_id ?? "",
              type: event.data.agent_type ?? "explore",
              description: event.data.description ?? "",
              status: "running" as const,
              tools: [],
              streamingText: "",
              resultSummary: "",
            },
          ];
          streamAgentsRef.current = updated;
          return updated;
        });
        break;

      case EventType.SUBAGENT_TOOL_START:
        setStreamAgents((agents) => {
          const updated = agents.map((a) =>
            a.id === event.data.agent_id
              ? {
                  ...a,
                  tools: [
                    ...a.tools,
                    { name: event.data.tool_name ?? "", status: "running", summary: undefined },
                  ],
                }
              : a,
          );
          streamAgentsRef.current = updated;
          return updated;
        });
        break;

      case EventType.SUBAGENT_TOOL_RESULT:
        setStreamAgents((agents) => {
          const updated = agents.map((a) => {
            if (a.id !== event.data.agent_id) return a;
            const tools = [...a.tools];
            const last = tools.findLast((t) => t.status === "running");
            if (last) {
              last.status = event.data.success ? "done" : "error";
              last.summary = event.data.summary ?? "";
            }
            return { ...a, tools };
          });
          streamAgentsRef.current = updated;
          return updated;
        });
        break;

      case EventType.SUBAGENT_TEXT_DELTA:
        setStreamAgents((agents) => {
          const updated = agents.map((a) =>
            a.id === event.data.agent_id
              ? { ...a, streamingText: a.streamingText + (event.data.text ?? "") }
              : a,
          );
          streamAgentsRef.current = updated;
          return updated;
        });
        break;

      case EventType.SUBAGENT_FINISHED:
        setStreamAgents((agents) => {
          const updated = agents.map((a) =>
            a.id === event.data.agent_id
              ? {
                  ...a,
                  status: (event.data.status ?? "done") as any,
                  resultSummary: event.data.result_summary ?? "",
                  durationMs: event.data.duration_ms,
                }
              : a,
          );
          streamAgentsRef.current = updated;
          return updated;
        });
        break;

      case EventType.ERROR:
        setStreamText((t) => {
          const msg = `\nError: ${event.data.error ?? event.data.message ?? "Unknown"}`;
          streamTextRef.current = t + msg;
          return t + msg;
        });
        break;
    }
  }, []);

  // ─── Send message ─────────────────────────────────────────────

  const handleSubmit = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || busy) return;
      setInput("");

      // Slash commands
      if (trimmed.startsWith("/")) {
        const result = parseSlashCommand(trimmed, {
          model,
          kbDatasetIds: kbIds,
          sessionId,
          baseUrl: config.base_url,
          messageCount: messages.length,
          mcpServers: mcpManager.servers,
        });

        if (result.handled) {
          // Simple actions
          if (result.action === "exit") { exit(); return; }
          if (result.action === "clear") { setMessages([]); return; }

          // /new
          if (result.action === "new") {
            setMessages([{ role: "system", content: "New conversation started." }]);
            setSessionId(undefined);
            setStreamText("");
            return;
          }

          // /compact
          if (result.action === "compact") {
            const userMsgs = messages.filter((m) => m.role !== "system");
            if (userMsgs.length <= 2) {
              sysMsg("Conversation too short to compact.");
              return;
            }
            const summary = userMsgs.map((m) => `${m.role}: ${m.content.slice(0, 200)}`).join("\n");
            setMessages([{ role: "system", content: `[Compacted ${messages.length} messages]\n\n${summary.slice(0, 1500)}` }]);
            return;
          }

          // /memory
          if (result.action === "memory") {
            if (result.memoryText === "__list__") {
              sysMsg(listMemories());
            } else if (result.memoryText) {
              saveMemory(result.memoryText);
              sysMsg(`Memory saved: "${result.memoryText}"`);
            }
            return;
          }

          // /history — fetch sessions from gateway
          if (result.action === "history") {
            setBusy(true);
            setStatusMsg("Fetching sessions...");
            try {
              const data = await client.request("GET", "/api/v1/assistant/sessions?limit=20");
              const sessions = Array.isArray(data) ? data : data.sessions ?? [];
              if (sessions.length === 0) {
                sysMsg("No conversation history found.");
              } else {
                const list = sessions.map((s: any) => {
                  const title = s.metadata?.title || s.title || "Untitled";
                  const date = (s.updated_at || s.created_at || "").slice(0, 16);
                  const id = s.session_id || s.id;
                  return `  ${id.slice(0, 8)}...  ${title}  (${date})`;
                }).join("\n");
                sysMsg(`Recent Conversations:\n${list}\n\nResume with: /resume <session_id>`);
              }
            } catch (err: any) {
              sysMsg(`Failed to fetch history: ${err.message}`);
            }
            setBusy(false);
            setStatusMsg("");
            return;
          }

          // /resume — load previous session
          if (result.action === "resume" && result.resumeSessionId) {
            const sid = result.resumeSessionId;
            setBusy(true);
            setStatusMsg("Loading session...");
            try {
              const data = await client.request("GET", `/api/v1/assistant/sessions/${sid}/messages`);
              const msgs = Array.isArray(data) ? data : data.messages ?? [];
              const loaded: Message[] = msgs.map((m: any) => ({
                role: m.role === "user" ? "user" as const : "assistant" as const,
                content: m.content || "",
              }));
              setSessionId(sid);
              setMessages([
                { role: "system", content: `Resumed session: ${sid.slice(0, 8)}... (${loaded.length} messages)` },
                ...loaded.slice(-20), // Show last 20 messages
              ]);
            } catch (err: any) {
              sysMsg(`Failed to resume: ${err.message}`);
            }
            setBusy(false);
            setStatusMsg("");
            return;
          }

          // /mcp connect
          if (result.action === "mcp_connect" && result.mcpName && result.mcpCommand) {
            setBusy(true);
            setStatusMsg(`Connecting MCP: ${result.mcpName}...`);
            try {
              const tools = await mcpManager.connect(
                result.mcpName,
                result.mcpCommand,
                result.mcpArgs ?? [],
              );
              sysMsg(
                `MCP connected: ${result.mcpName} (${tools.length} tools)\n` +
                tools.map((t) => `  - ${t.name}: ${t.description.slice(0, 60)}`).join("\n"),
              );
            } catch (err: any) {
              sysMsg(`MCP connect failed: ${err.message}`);
            }
            setBusy(false);
            setStatusMsg("");
            return;
          }

          // /mcp disconnect
          if (result.action === "mcp_disconnect" && result.mcpName) {
            await mcpManager.disconnect(result.mcpName);
            sysMsg(`MCP disconnected: ${result.mcpName}`);
            return;
          }

          // /mcp list
          if (result.action === "mcp_list") {
            const servers = mcpManager.servers;
            if (servers.length === 0) {
              sysMsg("No MCP servers connected.\nUsage: /mcp connect <name> <command> [args]");
            } else {
              const tools = mcpManager.listTools();
              sysMsg(
                `Connected MCP Servers: ${servers.join(", ")}\n\nTools (${tools.length}):\n` +
                tools.map((t) => `  ${t.name} (${t.server})`).join("\n"),
              );
            }
            return;
          }

          // /skill commands — full skill management
          if (result.action?.startsWith("skill_")) {
            setBusy(true);
            setStatusMsg("Processing skill command...");
            try {
              switch (result.action) {
                case "skill_list": {
                  const list = await listSkills(client);
                  sysMsg(`Installed Skills:\n${list}`);
                  break;
                }
                case "skill_create": {
                  const name = result.skillName!;
                  const title = result.skillArgs?.join(" ") || name;
                  // Create with a basic template — user can edit later
                  const res = await createAndUploadSkill(
                    client,
                    name,
                    title,
                    `Custom skill: ${title}`,
                    `When the user invokes this skill, follow these instructions:\n\n1. [Add your instructions here]\n2. [Describe the expected output]`,
                  );
                  sysMsg(`Skill created: ${res.name}\n${res.message}\n\nEdit via: /skill install <updated-file.md>`);
                  break;
                }
                case "skill_install": {
                  const filePath = result.skillName!;
                  const res = await uploadSkill(client, filePath);
                  sysMsg(`Skill installed: ${res.name}\n${res.message}`);
                  break;
                }
                case "skill_enable": {
                  const msg = await toggleSkill(client, result.skillName!, true);
                  sysMsg(msg);
                  break;
                }
                case "skill_disable": {
                  const msg = await toggleSkill(client, result.skillName!, false);
                  sysMsg(msg);
                  break;
                }
                case "skill_delete": {
                  const msg = await deleteSkill(client, result.skillName!);
                  sysMsg(msg);
                  break;
                }
                case "skill_test": {
                  const input = result.skillArgs?.join(" ") ?? "";
                  const output = await testSkill(client, result.skillName!, input);
                  sysMsg(`Skill test result:\n${output}`);
                  break;
                }
              }
            } catch (err: any) {
              sysMsg(`Skill error: ${err.message}`);
            }
            setBusy(false);
            setStatusMsg("");
            return;
          }

          // /kb list
          if (result.stateUpdates?._fetchKBList) {
            setBusy(true);
            setStatusMsg("Fetching knowledge bases...");
            try {
              const data = await client.request("GET", "/api/v1/knowledge/datasets");
              const datasets = Array.isArray(data) ? data : data.datasets ?? [];
              const list = datasets
                .map((d: any) => `  ${d.dataset_id}: ${d.name} (${d.total_documents ?? 0} docs)`)
                .join("\n");
              sysMsg(datasets.length
                ? `Available Knowledge Bases:\n${list}\n\nBind with: /kb <id1>, <id2>`
                : "No knowledge bases found.");
            } catch (err: any) {
              sysMsg(`Failed to fetch KBs: ${err.message}`);
            }
            setBusy(false);
            setStatusMsg("");
            return;
          }

          // Generic state updates
          if (result.stateUpdates) {
            if ("model" in result.stateUpdates) setModel(result.stateUpdates.model);
            if ("kbDatasetIds" in result.stateUpdates) setKbIds(result.stateUpdates.kbDatasetIds);
            if ("sessionId" in result.stateUpdates) setSessionId(result.stateUpdates.sessionId);
          }
          if (result.output) sysMsg(result.output);
          return;
        }
      }

      // Helper to add system message
      function sysMsg(content: string) {
        setMessages((m) => [...m, { role: "system" as const, content }]);
      }

      // Regular message
      setMessages((m) => [...m, { role: "user", content: trimmed }]);
      setBusy(true);
      setStreamText("");
      setStreamTools([]);
      setStreamAgents([]);
      setStatusMsg("Thinking...");
      streamTextRef.current = "";
      streamToolsRef.current = [];
      streamAgentsRef.current = [];

      try {
        for await (const event of chat.stream(trimmed, {
          sessionId,
          modelId: model,
          kbDatasetIds: kbIds.length > 0 ? kbIds : undefined,
          webSearchEnabled: true,
        })) {
          handleEvent(event);
        }
      } catch (err: any) {
        const errMsg = `Error: ${err.message}`;
        streamTextRef.current += (streamTextRef.current ? "\n" : "") + errMsg;
        setStreamText(streamTextRef.current);
      }

      // Commit as assistant message
      const finalText = streamTextRef.current;
      const finalTools = streamToolsRef.current;
      const finalAgents = streamAgentsRef.current;

      if (finalText || finalTools.length || finalAgents.length) {
        setMessages((m) => [
          ...m,
          {
            role: "assistant",
            content: finalText,
            tools: finalTools.length > 0 ? finalTools : undefined,
            agents: finalAgents.length > 0 ? finalAgents : undefined,
          },
        ]);
      } else {
        // Show error if nothing was received
        setMessages((m) => [
          ...m,
          { role: "system", content: "No response received. Check API key and base URL." },
        ]);
      }

      setBusy(false);
      setStreamText("");
      setStreamTools([]);
      setStreamAgents([]);
      setStatusMsg("");
    },
    [busy, model, kbIds, sessionId, config.base_url, chat, exit, handleConfirm, handleEvent],
  );

  // ─── Keyboard ─────────────────────────────────────────────────

  useInput((ch, key) => {
    // Ctrl+C to exit
    if (key.ctrl && ch === "c") {
      exit();
      return;
    }

    // Handle confirmation prompt
    if (confirm) {
      if (ch === "y" || ch === "Y") {
        const { resolve } = confirm;
        setConfirm(null);
        resolve(true);
      } else if (ch === "n" || ch === "N" || key.escape) {
        const { resolve } = confirm;
        setConfirm(null);
        resolve(false);
      }
    }
  });

  // ─── Render ───────────────────────────────────────────────────

  return (
    <Box flexDirection="column" padding={1}>
      {/* Header */}
      <Box marginBottom={1}>
        <Text bold color="cyan">
          Hejaz AI CLI v1.3.1
        </Text>
        <Text color="gray">
          {" "}
          | {model}
          {kbIds.length > 0 ? ` | KB: ${kbIds.join(", ")}` : ""}
          {sessionId ? ` | ${sessionId.slice(0, 8)}...` : ""}
        </Text>
      </Box>

      {/* Message History */}
      {messages.map((msg, i) => (
        <MessageView key={i} message={msg} />
      ))}

      {/* Live Streaming */}
      {busy && (
        <Box flexDirection="column" marginTop={1}>
          {/* Sub-agents */}
          {streamAgents.map((agent, i) => (
            <SubAgentView key={i} agent={agent} />
          ))}

          {/* Tool calls */}
          {streamTools.map((tool, i) => (
            <ToolCallView key={i} tool={tool} />
          ))}

          {/* Status */}
          {statusMsg && !streamText && (
            <Box marginLeft={2}>
              <Text color="gray">{statusMsg}</Text>
            </Box>
          )}

          {/* Streaming text */}
          {streamText && (
            <Box>
              <Text color="cyan" bold>
                AI:{" "}
              </Text>
              <Text wrap="wrap">{streamText}</Text>
            </Box>
          )}
        </Box>
      )}

      {/* Confirmation Prompt */}
      {confirm && (
        <Box
          flexDirection="column"
          marginTop={1}
          borderStyle="round"
          borderColor="yellow"
          paddingX={1}
        >
          <Text color="yellow" bold>
            Permission Required
          </Text>
          <Text color="white">{confirm.summary}</Text>
          <Box marginTop={1}>
            <Text color="green" bold>
              [y]
            </Text>
            <Text> Approve  </Text>
            <Text color="red" bold>
              [n]
            </Text>
            <Text> Deny</Text>
          </Box>
        </Box>
      )}

      {/* Input Line */}
      <Box marginTop={1}>
        <Text color="green" bold>
          {">"}{" "}
        </Text>
        {busy ? (
          <Text color="gray">
            {confirm ? "Waiting for permission..." : "Processing..."}
          </Text>
        ) : (
          <TextInput
            value={input}
            onChange={setInput}
            onSubmit={handleSubmit}
            placeholder="Type a message or /help..."
          />
        )}
      </Box>
    </Box>
  );
}

// ─── Sub-components ──────────────────────────────────────────────────

function MessageView({ message }: { message: Message }) {
  const { role, content, tools, agents } = message;

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box>
        {role === "user" && (
          <Text color="blue" bold>
            You:{" "}
          </Text>
        )}
        {role === "assistant" && (
          <Text color="cyan" bold>
            AI:{" "}
          </Text>
        )}
        {role === "system" && (
          <Text color="yellow" bold>
            System:{" "}
          </Text>
        )}
        <Text wrap="wrap">{content}</Text>
      </Box>

      {/* Sub-agents */}
      {agents?.map((agent, i) => (
        <SubAgentView key={i} agent={agent} compact />
      ))}

      {/* Tool calls */}
      {tools?.map((tool, i) => (
        <ToolCallView key={i} tool={tool} compact />
      ))}
    </Box>
  );
}

function ToolCallView({
  tool,
  compact,
}: {
  tool: ToolDisplay;
  compact?: boolean;
}) {
  const icon =
    tool.status === "running"
      ? "..."
      : tool.status === "done"
        ? "v"
        : tool.status === "denied"
          ? "x"
          : "!";
  const color =
    tool.status === "running"
      ? "yellow"
      : tool.status === "done"
        ? "green"
        : "red";

  return (
    <Box marginLeft={compact ? 4 : 2}>
      <Text color={color}>
        {icon} {tool.name}
      </Text>
      {tool.durationMs !== undefined && (
        <Text color="gray"> ({tool.durationMs}ms)</Text>
      )}
      {tool.result && !compact && (
        <Text color="gray"> — {truncate(tool.result, 60)}</Text>
      )}
    </Box>
  );
}

const AGENT_ICONS: Record<string, string> = {
  explore: "Search",
  task: "Task",
  plan: "Plan",
};

function SubAgentView({
  agent,
  compact,
}: {
  agent: AgentDisplay;
  compact?: boolean;
}) {
  const icon = AGENT_ICONS[agent.type] ?? agent.type;
  const statusIcon =
    agent.status === "running" ? "..." : agent.status === "done" ? "v" : "x";
  const statusColor =
    agent.status === "running"
      ? "yellow"
      : agent.status === "done"
        ? "green"
        : "red";

  return (
    <Box flexDirection="column" marginLeft={compact ? 4 : 2} marginBottom={compact ? 0 : 1}>
      {/* Header */}
      <Box>
        <Text color={statusColor}>{statusIcon} </Text>
        <Text color="magenta" bold>
          {icon}
        </Text>
        {agent.description && (
          <Text color="gray"> {agent.description}</Text>
        )}
        {agent.durationMs !== undefined && (
          <Text color="gray"> ({(agent.durationMs / 1000).toFixed(1)}s)</Text>
        )}
      </Box>

      {/* Tool timeline */}
      {!compact &&
        agent.tools.map((tool, i) => (
          <Box key={i} marginLeft={4}>
            <Text
              color={
                tool.status === "running"
                  ? "yellow"
                  : tool.status === "done"
                    ? "green"
                    : "red"
              }
            >
              {tool.status === "running" ? "..." : tool.status === "done" ? "v" : "x"}{" "}
              {tool.name}
            </Text>
            {tool.summary && (
              <Text color="gray"> — {truncate(tool.summary, 50)}</Text>
            )}
          </Box>
        ))}

      {/* Result summary (compact mode) */}
      {compact && agent.resultSummary && (
        <Box marginLeft={4}>
          <Text color="gray">{truncate(agent.resultSummary, 80)}</Text>
        </Box>
      )}

      {/* Streaming text (live mode) */}
      {!compact && agent.status === "running" && agent.streamingText && (
        <Box marginLeft={4}>
          <Text color="gray">{truncate(agent.streamingText, 200)}</Text>
        </Box>
      )}
    </Box>
  );
}

function truncate(str: string, max: number): string {
  if (str.length <= max) return str;
  return str.slice(0, max - 3) + "...";
}
