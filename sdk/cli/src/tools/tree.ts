/**
 * tree — Directory tree structure visualization.
 */

import { readdirSync, statSync } from "node:fs";
import { join, basename } from "node:path";

interface TreeArgs {
  path: string;
  depth?: number;
}

const SKIP_DIRS = new Set([
  "node_modules", ".git", "__pycache__", ".venv", "venv",
  ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
  ".next", ".nuxt", ".svelte-kit",
]);

export function tree(args: TreeArgs): string {
  const { path: rootPath, depth = 3 } = args;
  const lines: string[] = [basename(rootPath) + "/"];
  let fileCount = 0;
  let dirCount = 0;

  function walk(dir: string, prefix: string, level: number) {
    if (level >= depth) return;

    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    // Filter and sort: dirs first, then files
    const filtered = entries.filter((e) => !e.name.startsWith(".") || e.name === ".env.example");
    const dirs = filtered.filter((e) => e.isDirectory() && !SKIP_DIRS.has(e.name));
    const files = filtered.filter((e) => !e.isDirectory());
    const sorted = [...dirs, ...files];

    sorted.forEach((entry, i) => {
      const isLast = i === sorted.length - 1;
      const connector = isLast ? "└── " : "├── ";
      const childPrefix = isLast ? "    " : "│   ";

      if (entry.isDirectory()) {
        dirCount++;
        lines.push(`${prefix}${connector}${entry.name}/`);
        walk(join(dir, entry.name), prefix + childPrefix, level + 1);
      } else {
        fileCount++;
        const size = getFileSize(join(dir, entry.name));
        lines.push(`${prefix}${connector}${entry.name}${size ? ` (${size})` : ""}`);
      }
    });
  }

  walk(rootPath, "", 0);
  lines.push(`\n${dirCount} directories, ${fileCount} files`);

  return lines.join("\n");
}

function getFileSize(path: string): string {
  try {
    const stat = statSync(path);
    const b = stat.size;
    if (b < 1024) return `${b}B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)}K`;
    return `${(b / 1024 / 1024).toFixed(1)}M`;
  } catch {
    return "";
  }
}

export const treeDefinition = {
  name: "tree",
  description:
    "Show directory tree structure. Skips node_modules, .git, etc. Shows file sizes.",
  parameters: {
    path: {
      type: "string",
      description: "Root directory for the tree",
      required: true,
    },
    depth: {
      type: "number",
      description: "Max depth to traverse (default: 3)",
    },
  },
  category: "os" as const,
};
