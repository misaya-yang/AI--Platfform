/**
 * bash — Execute shell commands. High-risk: requires explicit confirmation.
 */

import { execSync } from "node:child_process";

interface BashArgs {
  command: string;
  timeout?: number;
}

export function bash(args: BashArgs): string {
  const { command, timeout = 120000 } = args;

  try {
    const output = execSync(command, {
      encoding: "utf-8",
      timeout,
      maxBuffer: 10 * 1024 * 1024, // 10MB
      cwd: process.cwd(),
      env: safeEnv(),
      shell: process.env.SHELL || "/bin/bash",
    });

    return output || "(no output)";
  } catch (err: any) {
    const stdout = err.stdout ?? "";
    const stderr = err.stderr ?? "";
    const code = err.status ?? err.signal ?? "unknown";
    return `Exit code ${code}\n${stdout}${stderr}`.trim();
  }
}

/** Strip sensitive env vars before passing to child process */
function safeEnv(): Record<string, string | undefined> {
  const env = { ...process.env };
  const sensitiveKeys = [
    "API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL",
    "AWS_SECRET", "PRIVATE_KEY", "NPM_TOKEN", "PYPI_TOKEN",
  ];
  for (const key of Object.keys(env)) {
    if (sensitiveKeys.some((s) => key.toUpperCase().includes(s))) {
      delete env[key];
    }
  }
  return env;
}

export const bashDefinition = {
  name: "bash",
  description:
    "Execute a shell command and return its output. Dangerous: requires user confirmation. Use for git, npm, build tools, etc.",
  parameters: {
    command: {
      type: "string",
      description: "The shell command to execute",
      required: true,
    },
    timeout: {
      type: "number",
      description: "Timeout in milliseconds (default: 120000 = 2 min)",
    },
  },
  category: "os" as const,
};
