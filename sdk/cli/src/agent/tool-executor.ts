/**
 * Unified tool executor — dispatches tool calls to OS tools or remote API.
 *
 * Flow:
 * 1. Check if tool is an OS tool → execute locally
 * 2. Check permissions before execution
 * 3. Otherwise, it's a remote tool → send back to gateway
 */

import { isOSTool, executeOSTool, getOSToolSchemas } from "../tools/index.js";
import { checkPermission, type PermissionCheck } from "../permissions.js";

export interface PendingToolCall {
  callId: string;
  name: string;
  arguments: Record<string, any>;
}

export interface ToolExecution {
  callId: string;
  name: string;
  result: string;
  durationMs: number;
  status: "success" | "error" | "denied";
}

export type ConfirmCallback = (
  summary: string,
  toolName: string,
) => Promise<boolean>;

/**
 * Execute a single tool call with permission checking.
 */
export async function executeTool(
  call: PendingToolCall,
  onConfirm?: ConfirmCallback,
): Promise<ToolExecution> {
  const start = Date.now();

  // Permission check for OS tools
  if (isOSTool(call.name)) {
    const perm = checkPermission(call.name, call.arguments);

    if (perm.action === "deny") {
      return {
        callId: call.callId,
        name: call.name,
        result: `Permission denied: ${perm.reason}`,
        durationMs: Date.now() - start,
        status: "denied",
      };
    }

    if (perm.action === "confirm") {
      if (!onConfirm) {
        return {
          callId: call.callId,
          name: call.name,
          result: "Permission denied: no confirmation handler available",
          durationMs: Date.now() - start,
          status: "denied",
        };
      }

      const approved = await onConfirm(perm.summary ?? call.name, call.name);
      if (!approved) {
        return {
          callId: call.callId,
          name: call.name,
          result: "User denied the operation",
          durationMs: Date.now() - start,
          status: "denied",
        };
      }
    }

    // Execute OS tool
    try {
      const result = await executeOSTool(call.name, call.arguments);
      return {
        callId: call.callId,
        name: call.name,
        result,
        durationMs: Date.now() - start,
        status: "success",
      };
    } catch (err: any) {
      return {
        callId: call.callId,
        name: call.name,
        result: `Error: ${err.message}`,
        durationMs: Date.now() - start,
        status: "error",
      };
    }
  }

  // Non-OS tools are remote — return marker for gateway handling
  return {
    callId: call.callId,
    name: call.name,
    result: `__REMOTE_TOOL__:${call.name}`,
    durationMs: 0,
    status: "success",
  };
}

export { getOSToolSchemas };
