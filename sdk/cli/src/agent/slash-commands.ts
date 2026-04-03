/**
 * Slash command parser — all CLI slash commands.
 */

export interface SlashCommandResult {
  handled: boolean;
  output?: string;
  action?:
    | "exit"
    | "clear"
    | "new"
    | "compact"
    | "memory"
    | "history"
    | "resume"
    | "mcp_connect"
    | "mcp_disconnect"
    | "mcp_list"
    | "skill_list"
    | "skill_create"
    | "skill_install"
    | "skill_enable"
    | "skill_disable"
    | "skill_delete"
    | "skill_test"
    | "none";
  stateUpdates?: Record<string, any>;
  memoryText?: string;
  resumeSessionId?: string;
  mcpName?: string;
  mcpCommand?: string;
  mcpArgs?: string[];
  /** For /skill */
  skillName?: string;
  skillArgs?: string[];
}

export function parseSlashCommand(
  input: string,
  state: {
    model: string;
    kbDatasetIds: string[];
    sessionId?: string;
    baseUrl: string;
    messageCount?: number;
    mcpServers?: string[];
  },
): SlashCommandResult {
  const trimmed = input.trim();
  if (!trimmed.startsWith("/")) return { handled: false };

  const parts = trimmed.split(/\s+/);
  const command = parts[0]!.toLowerCase();
  const args = parts.slice(1);

  switch (command) {
    case "/exit":
    case "/quit":
    case "/q":
      return { handled: true, action: "exit" };

    case "/clear":
      return { handled: true, action: "clear" };

    // ─── /new ────────────────────────────────────────────────
    case "/new":
      return {
        handled: true,
        action: "new",
        stateUpdates: { sessionId: undefined },
      };

    // ─── /compact ────────────────────────────────────────────
    case "/compact":
      if (!state.sessionId) {
        return { handled: true, output: "No active session to compact." };
      }
      return { handled: true, action: "compact" };

    // ─── /memory ─────────────────────────────────────────────
    case "/memory":
      if (args.length === 0) {
        return {
          handled: true,
          output:
            "Usage:\n" +
            "  /memory <note>      Save a memory\n" +
            "  /memory list        Show all memories\n" +
            "\nExample: /memory User prefers concise responses",
        };
      }
      if (args[0] === "list") {
        return { handled: true, action: "memory", memoryText: "__list__" };
      }
      return {
        handled: true,
        action: "memory",
        memoryText: args.join(" "),
      };

    // ─── /history ────────────────────────────────────────────
    case "/history":
    case "/sessions":
      return { handled: true, action: "history" };

    // ─── /resume ─────────────────────────────────────────────
    case "/resume":
      if (!args[0]) {
        return {
          handled: true,
          output: "Usage: /resume <session_id>\nUse /history to see available sessions.",
        };
      }
      return {
        handled: true,
        action: "resume",
        resumeSessionId: args[0],
      };

    // ─── /help ───────────────────────────────────────────────
    case "/help":
      return { handled: true, output: HELP_TEXT };

    // ─── /model ──────────────────────────────────────────────
    case "/model":
      if (args[0]) {
        return {
          handled: true,
          output: `Model switched to: ${args[0]}`,
          stateUpdates: { model: args[0] },
        };
      }
      return {
        handled: true,
        output: `Current model: ${state.model}\nUsage: /model <model-name>`,
      };

    // ─── /kb ─────────────────────────────────────────────────
    case "/kb":
      if (args[0] === "list") {
        return {
          handled: true,
          action: "none",
          stateUpdates: { _fetchKBList: true },
        };
      }
      if (args.length > 0) {
        const ids = args.join(" ").split(",").map((s) => s.trim()).filter(Boolean);
        return {
          handled: true,
          output: `Knowledge bases bound: ${ids.join(", ")}`,
          stateUpdates: { kbDatasetIds: ids },
        };
      }
      return {
        handled: true,
        output: state.kbDatasetIds.length
          ? `Current KBs: ${state.kbDatasetIds.join(", ")}\n  /kb list — show available | /kb id1, id2 — bind`
          : "No KBs bound. /kb list — show available | /kb id1, id2 — bind",
      };

    // ─── /session ────────────────────────────────────────────
    case "/session":
      if (args[0] === "new") {
        return { handled: true, action: "new", stateUpdates: { sessionId: undefined } };
      }
      return {
        handled: true,
        output: state.sessionId
          ? `Session: ${state.sessionId}\nMessages: ${state.messageCount ?? 0}`
          : "No active session (created on first message).",
      };

    // ─── /config ─────────────────────────────────────────────
    case "/config":
      return {
        handled: true,
        output: [
          "Configuration:",
          `  Model:    ${state.model}`,
          `  Base URL: ${state.baseUrl}`,
          `  KB:       ${state.kbDatasetIds.join(", ") || "none"}`,
          `  Session:  ${state.sessionId ?? "none"}`,
          `  MCP:      ${(state.mcpServers ?? []).join(", ") || "none"}`,
          "",
          "Edit: ~/.hejaz/config.json",
        ].join("\n"),
      };

    // ─── /mcp ────────────────────────────────────────────────
    case "/mcp":
      if (args[0] === "list") {
        return { handled: true, action: "mcp_list" };
      }
      if (args[0] === "connect" && args[1] && args[2]) {
        return {
          handled: true,
          action: "mcp_connect",
          mcpName: args[1],
          mcpCommand: args[2],
          mcpArgs: args.slice(3),
        };
      }
      if (args[0] === "disconnect" && args[1]) {
        return {
          handled: true,
          action: "mcp_disconnect",
          mcpName: args[1],
        };
      }
      return {
        handled: true,
        output:
          "MCP Server Management:\n" +
          "  /mcp list                          — List connected servers & tools\n" +
          "  /mcp connect <name> <cmd> [args]   — Connect a server\n" +
          "  /mcp disconnect <name>             — Disconnect\n" +
          "\nExamples:\n" +
          "  /mcp connect memory npx @modelcontextprotocol/server-memory\n" +
          "  /mcp connect github npx @modelcontextprotocol/server-github",
      };

    // ─── /skill ──────────────────────────────────────────────
    case "/skill":
      if (args[0] === "list") {
        return { handled: true, action: "skill_list" };
      }
      if (args[0] === "create" && args[1]) {
        // /skill create <name> [title...]
        return {
          handled: true,
          action: "skill_create",
          skillName: args[1],
          skillArgs: args.slice(2),
        };
      }
      if (args[0] === "install" && args[1]) {
        // /skill install <filepath>
        return {
          handled: true,
          action: "skill_install",
          skillName: args[1],
        };
      }
      if (args[0] === "enable" && args[1]) {
        return { handled: true, action: "skill_enable", skillName: args[1] };
      }
      if (args[0] === "disable" && args[1]) {
        return { handled: true, action: "skill_disable", skillName: args[1] };
      }
      if (args[0] === "delete" && args[1]) {
        return { handled: true, action: "skill_delete", skillName: args[1] };
      }
      if (args[0] === "test" && args[1] && args[2]) {
        return {
          handled: true,
          action: "skill_test",
          skillName: args[1],
          skillArgs: args.slice(2),
        };
      }
      return {
        handled: true,
        output:
          "Skill Management:\n" +
          "  /skill list                    — List installed skills\n" +
          "  /skill create <name> [title]   — Create a new skill interactively\n" +
          "  /skill install <file.md>       — Install from SKILL.md file\n" +
          "  /skill enable <name>           — Enable a skill\n" +
          "  /skill disable <name>          — Disable a skill\n" +
          "  /skill delete <name>           — Delete a skill\n" +
          "  /skill test <name> <input>     — Test a skill",
      };

    // ─── /share ──────────────────────────────────────────────
    case "/share":
      return {
        handled: true,
        output: state.sessionId
          ? `Share: ${state.baseUrl}/share/${state.sessionId}`
          : "No active session to share.",
      };

    case "/export":
      return { handled: true, output: "Export: /export markdown | /export json (coming soon)" };

    default:
      return {
        handled: true,
        output: `Unknown command: ${command}\nType /help for available commands.`,
      };
  }
}

const HELP_TEXT = `
Hejaz AI CLI Commands:

  Conversation
    /new                  Start new conversation
    /history              List recent conversations
    /resume <id>          Resume a previous conversation
    /compact              Compress conversation context
    /clear                Clear screen

  Knowledge & Memory
    /kb list              Show available knowledge bases
    /kb <id1, id2>        Bind knowledge bases
    /memory <note>        Save a persistent memory
    /memory list          Show saved memories

  Model & Config
    /model <name>         Switch LLM model
    /config               Show current config
    /session              Show session info

  MCP Servers
    /mcp list             List connected servers & tools
    /mcp connect <n> <cmd> [args]
    /mcp disconnect <n>

  Skills
    /skill list           List installed skills
    /skill create <name>  Create a new skill
    /skill install <file> Install from SKILL.md
    /skill enable|disable|delete <name>
    /skill test <name> <input>

  Other
    /share                Share conversation link
    /help                 Show this help
    /exit                 Quit
`.trim();
