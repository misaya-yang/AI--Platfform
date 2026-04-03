/**
 * edit_file — Exact string replacement in a file (like sed but exact match).
 */

import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { validatePath } from "../permissions.js";

interface EditFileArgs {
  file_path: string;
  old_string: string;
  new_string: string;
  replace_all?: boolean;
}

export function editFile(args: EditFileArgs): string {
  const { file_path, old_string, new_string, replace_all = false } = args;
  const absPath = resolve(file_path);
  const pathErr = validatePath(absPath);
  if (pathErr) return pathErr;

  const content = readFileSync(absPath, "utf-8");

  if (!content.includes(old_string)) {
    return `Error: old_string not found in ${absPath}`;
  }

  let updated: string;
  if (replace_all) {
    updated = content.split(old_string).join(new_string);
    const count = content.split(old_string).length - 1;
    writeFileSync(absPath, updated, "utf-8");
    return `Replaced ${count} occurrence(s) in ${absPath}`;
  }

  // Check uniqueness for single replacement
  const firstIdx = content.indexOf(old_string);
  const secondIdx = content.indexOf(old_string, firstIdx + 1);
  if (secondIdx !== -1) {
    return `Error: old_string matches multiple locations in ${absPath}. Use replace_all=true or provide more context.`;
  }

  updated = content.replace(old_string, new_string);
  writeFileSync(absPath, updated, "utf-8");
  return `Edited ${absPath} — replaced 1 occurrence`;
}

export const editFileDefinition = {
  name: "edit_file",
  description:
    "Perform exact string replacement in a file. old_string must be unique unless replace_all is true. Requires confirmation.",
  parameters: {
    file_path: {
      type: "string",
      description: "Absolute path to the file to edit",
      required: true,
    },
    old_string: {
      type: "string",
      description: "The exact text to find and replace",
      required: true,
    },
    new_string: {
      type: "string",
      description: "The replacement text",
      required: true,
    },
    replace_all: {
      type: "boolean",
      description: "Replace all occurrences (default: false)",
    },
  },
  category: "os" as const,
};
