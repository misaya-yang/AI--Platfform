/**
 * glob — Fast file pattern matching using fast-glob.
 */

import fg from "fast-glob";

interface GlobArgs {
  pattern: string;
  path?: string;
}

export async function globTool(args: GlobArgs): Promise<string> {
  const { pattern, path: searchPath } = args;
  const cwd = searchPath ?? process.cwd();

  const files = await fg(pattern, {
    cwd,
    dot: false,
    onlyFiles: true,
    absolute: true,
    suppressErrors: true,
    ignore: ["**/node_modules/**", "**/.git/**"],
  });

  if (files.length === 0) {
    return `No files matched: ${pattern}`;
  }

  // Sort by path and limit output
  files.sort();
  const limited = files.slice(0, 200);
  const result = limited.join("\n");

  return files.length > 200
    ? `${result}\n\n... and ${files.length - 200} more files`
    : result;
}

export const globDefinition = {
  name: "glob",
  description:
    "Search for files matching a glob pattern (e.g. '**/*.ts', 'src/**/*.tsx'). Returns matching file paths.",
  parameters: {
    pattern: {
      type: "string",
      description: "Glob pattern to match (e.g. '**/*.ts')",
      required: true,
    },
    path: {
      type: "string",
      description: "Directory to search in. Defaults to current working directory.",
    },
  },
  category: "os" as const,
};
