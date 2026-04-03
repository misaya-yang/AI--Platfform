/**
 * HEJAZ.md hierarchical project memory system.
 *
 * Discovery order:
 * 1. ~/.hejaz/HEJAZ.md (global)
 * 2. <project-root>/HEJAZ.md (project-level)
 * 3. <project-root>/.hejaz/rules/*.md (modular rules)
 *
 * All found content is concatenated and injected into system prompt.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export function loadProjectMemory(): string {
  const parts: string[] = [];

  // 1. Global HEJAZ.md
  const globalPath = join(homedir(), ".hejaz", "HEJAZ.md");
  if (existsSync(globalPath)) {
    parts.push(`[Global Instructions]\n${readFileSync(globalPath, "utf-8").trim()}`);
  }

  // 2. Project-level HEJAZ.md (walk up from cwd)
  const projectFile = findUpward("HEJAZ.md", process.cwd());
  if (projectFile) {
    parts.push(`[Project Instructions]\n${readFileSync(projectFile, "utf-8").trim()}`);
  }

  // 3. .hejaz/rules/*.md in project root
  const projectRoot = projectFile ? resolve(projectFile, "..") : process.cwd();
  const rulesDir = join(projectRoot, ".hejaz", "rules");
  if (existsSync(rulesDir)) {
    try {
      const files = readdirSync(rulesDir).filter((f) => f.endsWith(".md")).sort();
      for (const file of files) {
        const content = readFileSync(join(rulesDir, file), "utf-8").trim();
        if (content) parts.push(`[Rule: ${file}]\n${content}`);
      }
    } catch {
      // Skip if unreadable
    }
  }

  // 4. Load memories.json entries
  const memoriesPath = join(homedir(), ".hejaz", "memories.json");
  if (existsSync(memoriesPath)) {
    try {
      const memories = JSON.parse(readFileSync(memoriesPath, "utf-8"));
      if (Array.isArray(memories) && memories.length > 0) {
        const entries = memories.map((m: any) => `- ${m.text}`).join("\n");
        parts.push(`[User Memories]\n${entries}`);
      }
    } catch {
      // Skip
    }
  }

  return parts.length > 0 ? parts.join("\n\n") : "";
}

function findUpward(filename: string, startDir: string): string | null {
  let dir = resolve(startDir);
  const root = resolve("/");
  while (dir !== root) {
    const candidate = join(dir, filename);
    if (existsSync(candidate)) return candidate;
    const parent = resolve(dir, "..");
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}
