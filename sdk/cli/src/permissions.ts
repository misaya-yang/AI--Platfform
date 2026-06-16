/**
 * Permission system — three-level access control for OS tools.
 *
 * Level 1 (auto): read_file, glob, grep, list_dir, tree — auto-approved
 * Level 2 (confirm): write_file, edit_file — show diff, require y/n
 * Level 3 (dangerous): bash — show command, require y/n with warning
 */

import { resolve, relative } from "node:path";
import { loadPermissions } from "./config.js";
import type { PermissionsConfig } from "./types/config.js";

export type PermissionAction = "allow" | "deny" | "confirm";

export interface PermissionCheck {
  action: PermissionAction;
  reason?: string;
  /** For confirm actions, a summary of what will happen */
  summary?: string;
}

let cachedPerms: PermissionsConfig | null = null;

function getPerms(): PermissionsConfig {
  if (!cachedPerms) cachedPerms = loadPermissions();
  return cachedPerms;
}

export function reloadPermissions() {
  cachedPerms = null;
}

/**
 * Defense-in-depth path validation for tool functions.
 * Returns an error string if path is blocked, or null if OK.
 */
export function validatePath(absPath: string): string | null {
  const perms = getPerms();

  // Check blocked patterns
  for (const pattern of perms.blocked_patterns) {
    if (matchGlob(absPath, pattern)) {
      return `Error: path blocked by pattern: ${pattern}`;
    }
  }

  // Must be within cwd or allowed_dirs
  const cwd = process.cwd();
  const inCwd = absPath.startsWith(cwd + "/") || absPath === cwd;

  if (perms.allowed_dirs.length > 0) {
    const inAllowed = perms.allowed_dirs.some((dir) => {
      const absDir = resolve(dir);
      return absPath.startsWith(absDir + "/") || absPath === absDir;
    });
    if (!inAllowed && !inCwd) {
      return `Error: path outside allowed directories: ${absPath}`;
    }
  } else if (!inCwd) {
    return `Error: path outside working directory: ${absPath}`;
  }

  return null;
}

/**
 * Check if a tool call should be auto-approved, require confirmation, or be denied.
 */
export function checkPermission(
  toolName: string,
  args: Record<string, any>,
): PermissionCheck {
  const perms = getPerms();

  // Check auto-approve list
  if (perms.auto_approve.includes(toolName)) {
    // Still check file path access for read tools
    if (args.file_path || args.path) {
      const pathCheck = checkPathAccess(args.file_path ?? args.path, perms);
      if (pathCheck) return pathCheck;
    }
    return { action: "allow" };
  }

  // Check if tool requires confirmation
  if (perms.require_confirm.includes(toolName)) {
    // First check path access
    if (args.file_path) {
      const pathCheck = checkPathAccess(args.file_path, perms);
      if (pathCheck) return pathCheck;
    }

    return {
      action: "confirm",
      summary: buildConfirmSummary(toolName, args),
    };
  }

  // Default: require confirmation for unknown tools
  return {
    action: "confirm",
    summary: buildConfirmSummary(toolName, args),
  };
}

/**
 * Check if a path is accessible.
 */
function checkPathAccess(
  filePath: string,
  perms: PermissionsConfig,
): PermissionCheck | null {
  const absPath = resolve(filePath);

  // Check blocked patterns
  for (const pattern of perms.blocked_patterns) {
    if (matchGlob(absPath, pattern)) {
      return {
        action: "deny",
        reason: `Path matches blocked pattern: ${pattern}`,
      };
    }
  }

  // If allowed_dirs is configured, check that path falls within them
  if (perms.allowed_dirs.length > 0) {
    const inAllowed = perms.allowed_dirs.some((dir) => {
      const absDir = resolve(dir);
      return absPath.startsWith(absDir + "/") || absPath === absDir;
    });
    if (!inAllowed) {
      // Allow access to cwd and subdirs always
      const cwd = process.cwd();
      if (!absPath.startsWith(cwd + "/") && absPath !== cwd) {
        return {
          action: "deny",
          reason: `Path outside allowed directories. Add to ~/.ai-gateway/permissions.json allowed_dirs`,
        };
      }
    }
  }

  return null;
}

function matchGlob(path: string, pattern: string): boolean {
  // Simple glob matching for blocked patterns
  const escaped = pattern
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*/g, "DOUBLESTAR")
    .replace(/\*/g, "[^/]*")
    .replace(/DOUBLESTAR/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(escaped).test(path);
}

function buildConfirmSummary(
  toolName: string,
  args: Record<string, any>,
): string {
  switch (toolName) {
    case "write_file":
      return `Write file: ${args.file_path} (${(args.content?.length ?? 0)} chars)`;
    case "edit_file":
      return `Edit file: ${args.file_path}\n  - ${(args.old_string ?? "").slice(0, 50)}...\n  + ${(args.new_string ?? "").slice(0, 50)}...`;
    case "bash":
      return `Execute: ${args.command}`;
    default:
      return `${toolName}: ${JSON.stringify(args).slice(0, 100)}`;
  }
}
