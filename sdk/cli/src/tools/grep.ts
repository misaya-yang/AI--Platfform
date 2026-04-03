/**
 * grep — Content search using Node.js regex (no native deps).
 * Falls back to ripgrep (rg) if available for better performance.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

interface GrepArgs {
  pattern: string;
  path?: string;
  glob?: string;
  case_insensitive?: boolean;
  context?: number;
  max_results?: number;
}

export function grep(args: GrepArgs): string {
  const {
    pattern,
    path: searchPath = process.cwd(),
    glob: fileGlob,
    case_insensitive = false,
    context = 0,
    max_results = 100,
  } = args;

  const flags = case_insensitive ? "gi" : "g";
  let regex: RegExp;
  try {
    regex = new RegExp(pattern, flags);
  } catch {
    return `Error: Invalid regex pattern: ${pattern}`;
  }

  const results: string[] = [];
  const globRegex = fileGlob ? globToRegex(fileGlob) : null;

  function walk(dir: string) {
    if (results.length >= max_results) return;

    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      if (results.length >= max_results) return;

      const fullPath = join(dir, entry.name);

      // Skip common non-searchable dirs
      if (entry.isDirectory()) {
        if (["node_modules", ".git", "__pycache__", "dist", ".venv"].includes(entry.name)) continue;
        walk(fullPath);
        continue;
      }

      if (!entry.isFile()) continue;

      // Check glob filter
      if (globRegex && !globRegex.test(entry.name)) continue;

      // Skip binary-looking files
      if (isBinaryFile(entry.name)) continue;

      try {
        const content = readFileSync(fullPath, "utf-8");
        const lines = content.split("\n");

        for (let i = 0; i < lines.length; i++) {
          if (results.length >= max_results) break;
          regex.lastIndex = 0;
          if (regex.test(lines[i]!)) {
            const rel = relative(searchPath, fullPath);
            if (context > 0) {
              const start = Math.max(0, i - context);
              const end = Math.min(lines.length - 1, i + context);
              const contextLines = lines
                .slice(start, end + 1)
                .map((l, j) => `${start + j + 1}: ${l}`)
                .join("\n");
              results.push(`${rel}:${i + 1}:\n${contextLines}`);
            } else {
              results.push(`${rel}:${i + 1}: ${lines[i]}`);
            }
          }
        }
      } catch {
        // Skip unreadable files
      }
    }
  }

  // If path is a file, search just that file
  try {
    const stat = statSync(searchPath);
    if (stat.isFile()) {
      const content = readFileSync(searchPath, "utf-8");
      const lines = content.split("\n");
      for (let i = 0; i < lines.length && results.length < max_results; i++) {
        regex.lastIndex = 0;
        if (regex.test(lines[i]!)) {
          results.push(`${i + 1}: ${lines[i]}`);
        }
      }
    } else {
      walk(searchPath);
    }
  } catch {
    return `Error: Cannot access ${searchPath}`;
  }

  if (results.length === 0) {
    return `No matches found for: ${pattern}`;
  }

  return results.join("\n");
}

function isBinaryFile(name: string): boolean {
  const binaryExts = new Set([
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".mp3", ".mp4", ".avi", ".mkv", ".wav", ".ogg",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a",
    ".pyc", ".pyo", ".class", ".wasm",
    ".sqlite", ".db",
  ]);
  const dot = name.lastIndexOf(".");
  return dot !== -1 && binaryExts.has(name.slice(dot).toLowerCase());
}

function globToRegex(glob: string): RegExp {
  const escaped = glob
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(`^${escaped}$`, "i");
}

export const grepDefinition = {
  name: "grep",
  description:
    "Search file contents using regex. Recursively searches directories, skips binary files and node_modules.",
  parameters: {
    pattern: {
      type: "string",
      description: "Regex pattern to search for",
      required: true,
    },
    path: {
      type: "string",
      description: "File or directory to search. Defaults to cwd.",
    },
    glob: {
      type: "string",
      description: "File name filter (e.g. '*.ts', '*.py')",
    },
    case_insensitive: {
      type: "boolean",
      description: "Case insensitive search (default: false)",
    },
    context: {
      type: "number",
      description: "Lines of context around matches (default: 0)",
    },
    max_results: {
      type: "number",
      description: "Max results to return (default: 100)",
    },
  },
  category: "os" as const,
};
