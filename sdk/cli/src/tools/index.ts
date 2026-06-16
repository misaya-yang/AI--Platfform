/**
 * OS Agent tools — unified registry and executor.
 */

import { readFile, readFileDefinition } from "./read-file.js";
import { writeFile, writeFileDefinition } from "./write-file.js";
import { editFile, editFileDefinition } from "./edit-file.js";
import { globTool, globDefinition } from "./glob.js";
import { grep, grepDefinition } from "./grep.js";
import { bash, bashDefinition } from "./bash.js";
import { listDir, listDirDefinition } from "./list-dir.js";
import { tree, treeDefinition } from "./tree.js";
import { todoWrite, todoDefinition } from "./todo.js";
import type { ToolDefinition } from "../types/tools.js";

export const OS_TOOL_DEFINITIONS: ToolDefinition[] = [
  readFileDefinition,
  writeFileDefinition,
  editFileDefinition,
  globDefinition,
  grepDefinition,
  bashDefinition,
  listDirDefinition,
  treeDefinition,
  todoDefinition,
];

/**
 * Build the tools array for the LLM (OpenAI function-calling format).
 */
export function getOSToolSchemas() {
  return OS_TOOL_DEFINITIONS.map((def) => ({
    type: "function" as const,
    function: {
      name: def.name,
      description: def.description,
      parameters: {
        type: "object",
        properties: Object.fromEntries(
          Object.entries(def.parameters).map(([key, param]) => [
            key,
            {
              type: param.type,
              description: param.description,
              ...(param.default !== undefined ? { default: param.default } : {}),
              ...(param.enum ? { enum: param.enum } : {}),
            },
          ]),
        ),
        required: Object.entries(def.parameters)
          .filter(([, p]) => p.required)
          .map(([k]) => k),
      },
    },
  }));
}

/**
 * Execute an OS tool by name. Returns the result string.
 * Throws on unknown tool.
 */
export async function executeOSTool(
  name: string,
  args: Record<string, any>,
): Promise<string> {
  switch (name) {
    case "read_file":
      return readFile(args as any);
    case "write_file":
      return writeFile(args as any);
    case "edit_file":
      return editFile(args as any);
    case "glob":
      return globTool(args as any);
    case "grep":
      return grep(args as any);
    case "bash":
      return bash(args as any);
    case "list_dir":
      return listDir(args as any);
    case "tree":
      return tree(args as any);
    case "todo_write":
      return todoWrite(args as any);
    default:
      throw new Error(`Unknown OS tool: ${name}`);
  }
}

export function isOSTool(name: string): boolean {
  return OS_TOOL_DEFINITIONS.some((d) => d.name === name);
}
