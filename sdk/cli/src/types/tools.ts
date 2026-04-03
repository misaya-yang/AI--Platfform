/**
 * Tool definition types for OS Agent and remote tools.
 */

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, ToolParameter>;
  category: "os" | "remote" | "mcp";
}

export interface ToolParameter {
  type: string;
  description: string;
  required?: boolean;
  default?: any;
  enum?: string[];
}

export interface ToolCallRequest {
  toolName: string;
  arguments: Record<string, any>;
  callId: string;
}

export interface ToolCallResult {
  callId: string;
  toolName: string;
  result: any;
  error?: string;
  durationMs: number;
}

/** Permission levels */
export type PermissionLevel = "auto" | "confirm" | "dangerous";

export function getPermissionLevel(toolName: string): PermissionLevel {
  const autoApprove = new Set([
    "read_file",
    "glob",
    "grep",
    "list_dir",
    "tree",
    "search_knowledge_base",
    "search_web",
  ]);
  const dangerous = new Set(["bash"]);

  if (autoApprove.has(toolName)) return "auto";
  if (dangerous.has(toolName)) return "dangerous";
  return "confirm";
}
