/**
 * write_file — Create or overwrite a file.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { validatePath } from "../permissions.js";

interface WriteFileArgs {
  file_path: string;
  content: string;
}

export function writeFile(args: WriteFileArgs): string {
  const { file_path, content } = args;
  const absPath = resolve(file_path);
  const pathErr = validatePath(absPath);
  if (pathErr) return pathErr;

  mkdirSync(dirname(absPath), { recursive: true });
  writeFileSync(absPath, content, "utf-8");

  const lines = content.split("\n").length;
  const bytes = Buffer.byteLength(content, "utf-8");
  return `File written: ${absPath} (${lines} lines, ${bytes} bytes)`;
}

export const writeFileDefinition = {
  name: "write_file",
  description:
    "Create or overwrite a file. Parent directories are created automatically. Requires user confirmation.",
  parameters: {
    file_path: {
      type: "string",
      description: "Absolute path to the file to write",
      required: true,
    },
    content: {
      type: "string",
      description: "The content to write to the file",
      required: true,
    },
  },
  category: "os" as const,
};
