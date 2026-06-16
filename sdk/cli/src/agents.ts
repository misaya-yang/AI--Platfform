/**
 * Custom agent definitions — .ai-gateway/agents/*.md
 *
 * Agent files use YAML frontmatter:
 * ---
 * name: code-reviewer
 * description: Review code for bugs
 * model: qwen3.6-plus
 * tools: [read_file, glob, grep]
 * maxTurns: 10
 * ---
 * ## Instructions
 * You are a code reviewer...
 */

import { existsSync, readFileSync, readdirSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface AgentDef {
  name: string;
  description: string;
  model?: string;
  tools?: string[];
  maxTurns?: number;
  instructions: string;
  source: "builtin" | "global" | "project";
}

// Built-in agent types (matches backend SubAgentType)
const BUILTIN_AGENTS: AgentDef[] = [
  {
    name: "explore",
    description: "Fast search — read-only tools (KB, web, file search)",
    model: undefined,
    tools: ["read_file", "glob", "grep", "list_dir", "tree", "search_knowledge_base", "search_web"],
    maxTurns: 8,
    instructions: "You are an Explore agent. Search and read files to answer the question. Do not modify anything.",
    source: "builtin",
  },
  {
    name: "task",
    description: "Full execution — all tools available",
    model: undefined,
    tools: undefined, // all
    maxTurns: 15,
    instructions: "You are a Task agent. Execute the requested task using all available tools.",
    source: "builtin",
  },
  {
    name: "plan",
    description: "Analyze and create implementation plan",
    model: undefined,
    tools: ["read_file", "glob", "grep", "list_dir", "tree"],
    maxTurns: 5,
    instructions: "You are a Plan agent. Analyze the codebase and create a step-by-step implementation plan. Do not execute — only plan.",
    source: "builtin",
  },
];

/**
 * Load all agent definitions: builtin + global (~/.ai-gateway/agents/) + project (.ai-gateway/agents/)
 */
export function loadAgents(): AgentDef[] {
  const agents = [...BUILTIN_AGENTS];

  // Global agents
  const globalDir = join(homedir(), ".ai-gateway", "agents");
  loadAgentsFromDir(globalDir, "global", agents);

  // Project agents
  const projectDir = join(process.cwd(), ".ai-gateway", "agents");
  loadAgentsFromDir(projectDir, "project", agents);

  return agents;
}

function loadAgentsFromDir(dir: string, source: "global" | "project", agents: AgentDef[]) {
  if (!existsSync(dir)) return;
  try {
    const files = readdirSync(dir).filter((f) => f.endsWith(".md"));
    for (const file of files) {
      const content = readFileSync(join(dir, file), "utf-8");
      const agent = parseAgentMd(content, source);
      if (agent) {
        // Replace if same name exists
        const idx = agents.findIndex((a) => a.name === agent.name);
        if (idx >= 0) agents[idx] = agent;
        else agents.push(agent);
      }
    }
  } catch {
    // Skip unreadable
  }
}

function parseAgentMd(content: string, source: "global" | "project"): AgentDef | null {
  const fmMatch = content.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!fmMatch) return null;

  const frontmatter = fmMatch[1]!;
  const body = fmMatch[2]!.trim();

  const get = (key: string): string | undefined => {
    const m = frontmatter.match(new RegExp(`^${key}:\\s*(.+)$`, "m"));
    return m?.[1]?.trim();
  };

  const name = get("name");
  if (!name) return null;

  const toolsRaw = get("tools");
  let tools: string[] | undefined;
  if (toolsRaw) {
    // Parse [a, b, c] or YAML list
    tools = toolsRaw.replace(/[\[\]]/g, "").split(",").map((s) => s.trim()).filter(Boolean);
  }

  return {
    name,
    description: get("description") ?? name,
    model: get("model"),
    tools,
    maxTurns: parseInt(get("maxTurns") ?? "10", 10),
    instructions: body,
    source,
  };
}

export function listAgentsSummary(): string {
  const agents = loadAgents();
  return agents.map((a) => {
    const badge = a.source === "builtin" ? "[builtin]" : a.source === "global" ? "[global]" : "[project]";
    return `  ${a.name} ${badge}: ${a.description}`;
  }).join("\n");
}

export function getAgent(name: string): AgentDef | undefined {
  return loadAgents().find((a) => a.name === name);
}
