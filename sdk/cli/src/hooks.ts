/**
 * Hooks system — Pre/Post tool execution hooks.
 *
 * Config: ~/.hejaz/hooks.json
 *
 * Supported events:
 * - PreToolUse: runs before tool execution, can deny (exit 2) or transform args
 * - PostToolUse: runs after tool execution, can run formatters/linters
 *
 * Handler types:
 * - command: execute shell command ($TOOL_NAME, $FILE_PATH, $ARGUMENTS available)
 * - deny: block execution with message
 */

import { existsSync, readFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

interface HookRule {
  matcher: {
    tool?: string;
    tool_pattern?: string;
    command_pattern?: string;
    glob?: string;
  };
  action: "command" | "deny" | "allow";
  command?: string;
  message?: string;
}

interface HooksConfig {
  PreToolUse?: HookRule[];
  PostToolUse?: HookRule[];
}

const HOOKS_FILE = join(homedir(), ".hejaz", "hooks.json");

let cachedHooks: HooksConfig | null = null;

function loadHooks(): HooksConfig {
  if (cachedHooks) return cachedHooks;
  try {
    if (existsSync(HOOKS_FILE)) {
      const raw = JSON.parse(readFileSync(HOOKS_FILE, "utf-8"));
      cachedHooks = raw.hooks ?? raw;
      return cachedHooks!;
    }
  } catch {
    // Invalid config — no hooks
  }
  cachedHooks = {};
  return cachedHooks;
}

function matchesRule(rule: HookRule, toolName: string, args: Record<string, any>): boolean {
  // Match tool name
  if (rule.matcher.tool && rule.matcher.tool !== toolName) return false;
  if (rule.matcher.tool_pattern) {
    try {
      if (!new RegExp(rule.matcher.tool_pattern).test(toolName)) return false;
    } catch { return false; }
  }

  // Match command content (for bash tool)
  if (rule.matcher.command_pattern && toolName === "bash") {
    const cmd = args.command ?? "";
    try {
      if (!new RegExp(rule.matcher.command_pattern).test(cmd)) return false;
    } catch { return false; }
  }

  // Match file path glob
  if (rule.matcher.glob) {
    const filePath = args.file_path ?? args.path ?? "";
    if (!filePath.includes(rule.matcher.glob.replace(/\*/g, ""))) return false;
  }

  return true;
}

export interface HookResult {
  allowed: boolean;
  message?: string;
}

/**
 * Run PreToolUse hooks. Returns whether the tool should proceed.
 */
export function runPreToolHooks(
  toolName: string,
  args: Record<string, any>,
): HookResult {
  const hooks = loadHooks();
  const rules = hooks.PreToolUse ?? [];

  for (const rule of rules) {
    if (!matchesRule(rule, toolName, args)) continue;

    if (rule.action === "deny") {
      return { allowed: false, message: rule.message ?? `Blocked by hook: ${toolName}` };
    }

    if (rule.action === "command" && rule.command) {
      try {
        const env = {
          ...process.env,
          TOOL_NAME: toolName,
          FILE_PATH: args.file_path ?? args.path ?? "",
          ARGUMENTS: JSON.stringify(args),
        };
        const result = execSync(rule.command, { encoding: "utf-8", env, timeout: 10000 });
        // Exit code 0 = allow, 2 = deny (other = allow with warning)
      } catch (err: any) {
        if (err.status === 2) {
          return { allowed: false, message: err.stdout?.trim() || `Hook denied: ${rule.command}` };
        }
        // Non-blocking error — allow
      }
    }
  }

  return { allowed: true };
}

/**
 * Run PostToolUse hooks (e.g., auto-format after write).
 */
export function runPostToolHooks(
  toolName: string,
  args: Record<string, any>,
  result: string,
): void {
  const hooks = loadHooks();
  const rules = hooks.PostToolUse ?? [];

  for (const rule of rules) {
    if (!matchesRule(rule, toolName, args)) continue;

    if (rule.action === "command" && rule.command) {
      try {
        // Use environment variables (not string interpolation) to prevent command injection
        const env = {
          ...process.env,
          TOOL_NAME: toolName,
          FILE_PATH: args.file_path ?? args.path ?? "",
          ARGUMENTS: JSON.stringify(args),
          TOOL_RESULT: result.slice(0, 5000),
        };
        execSync(rule.command, { encoding: "utf-8", timeout: 15000, stdio: "ignore", env });
      } catch {
        // Non-blocking — silently continue
      }
    }
  }
}

export function reloadHooks() {
  cachedHooks = null;
}
