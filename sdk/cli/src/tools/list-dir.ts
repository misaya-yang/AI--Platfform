/**
 * list_dir — List directory contents with file sizes.
 */

import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

interface ListDirArgs {
  path: string;
}

export function listDir(args: ListDirArgs): string {
  const { path: dirPath } = args;

  const entries = readdirSync(dirPath, { withFileTypes: true });
  const lines: string[] = [];

  for (const entry of entries) {
    const fullPath = join(dirPath, entry.name);
    try {
      const stat = statSync(fullPath);
      const size = entry.isDirectory()
        ? "-"
        : formatSize(stat.size);
      const type = entry.isDirectory() ? "d" : entry.isSymbolicLink() ? "l" : "-";
      lines.push(`${type}  ${size.padStart(8)}  ${entry.name}${entry.isDirectory() ? "/" : ""}`);
    } catch {
      lines.push(`?  ${"-".padStart(8)}  ${entry.name}`);
    }
  }

  // Sort: directories first, then files
  lines.sort((a, b) => {
    const aDir = a.startsWith("d");
    const bDir = b.startsWith("d");
    if (aDir !== bDir) return aDir ? -1 : 1;
    return a.localeCompare(b);
  });

  return lines.length > 0
    ? `${dirPath} (${entries.length} items)\n${lines.join("\n")}`
    : `${dirPath} (empty)`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}M`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)}G`;
}

export const listDirDefinition = {
  name: "list_dir",
  description:
    "List directory contents with file types and sizes. Directories shown first.",
  parameters: {
    path: {
      type: "string",
      description: "Directory path to list",
      required: true,
    },
  },
  category: "os" as const,
};
