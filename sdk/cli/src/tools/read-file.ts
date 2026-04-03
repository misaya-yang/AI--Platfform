/**
 * read_file — Read file contents with optional line range.
 */

import { readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { validatePath } from "../permissions.js";

interface ReadFileArgs {
  file_path: string;
  offset?: number;
  limit?: number;
}

export function readFile(args: ReadFileArgs): string {
  const { file_path, offset = 0, limit = 2000 } = args;
  const absPath = resolve(file_path);
  const pathErr = validatePath(absPath);
  if (pathErr) return pathErr;

  const stat = statSync(absPath);
  if (stat.isDirectory()) {
    return `Error: ${absPath} is a directory, not a file.`;
  }

  const content = readFileSync(absPath, "utf-8");
  const lines = content.split("\n");

  const start = Math.max(0, offset);
  const end = Math.min(lines.length, start + limit);
  const selected = lines.slice(start, end);

  // Format with line numbers (cat -n style)
  const numbered = selected.map((line, i) => `${start + i + 1}\t${line}`);
  return numbered.join("\n");
}

export const readFileDefinition = {
  name: "read_file",
  description:
    "Read a file from the filesystem. Returns content with line numbers. Use offset/limit for large files.",
  parameters: {
    file_path: {
      type: "string",
      description: "Absolute path to the file to read",
      required: true,
    },
    offset: {
      type: "number",
      description: "Line number to start reading from (0-based). Default: 0",
    },
    limit: {
      type: "number",
      description: "Max number of lines to read. Default: 2000",
    },
  },
  category: "os" as const,
};
