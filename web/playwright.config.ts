import path from "path";
import { fileURLToPath } from "url";
import { defineConfig, devices } from "@playwright/test";

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required E2E env: ${name}`);
  }
  return value;
}

const baseURL = requireEnv("E2E_BASE_URL");
const apiURL = requireEnv("E2E_API_URL");
const knowledgeURL =
  process.env.E2E_KNOWLEDGE_URL ||
  `http://127.0.0.1:${process.env.E2E_KNOWLEDGE_PORT || "8092"}`;
const assistantURL =
  process.env.E2E_ASSISTANT_URL ||
  `http://127.0.0.1:${process.env.E2E_ASSISTANT_PORT || "8093"}`;
const mcpDocgenURL =
  process.env.E2E_MCP_DOCGEN_URL ||
  `http://127.0.0.1:${process.env.E2E_MCP_DOCGEN_PORT || "8765"}`;
const configDir = path.dirname(fileURLToPath(import.meta.url));
const stackScript = path.resolve(configDir, "../scripts/dev/start_e2e_stack.sh");
const authStatePath = path.resolve(configDir, ".playwright/auth-state.json");
const globalSetupPath = path.resolve(configDir, "e2e/global.setup.ts");

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  globalSetup: globalSetupPath,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    storageState: authStatePath,
    trace: "retain-on-first-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `${stackScript} mcp-docgen`,
      url: `${mcpDocgenURL}/health`,
      reuseExistingServer: !!(process.env.E2E_REUSE_SERVER),
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `${stackScript} knowledge`,
      url: `${knowledgeURL}/health/ready`,
      reuseExistingServer: !!(process.env.E2E_REUSE_SERVER),
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `${stackScript} assistant`,
      url: `${assistantURL}/health/ready`,
      reuseExistingServer: !!(process.env.E2E_REUSE_SERVER),
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `${stackScript} backend`,
      url: `${apiURL}/health`,
      reuseExistingServer: !!(process.env.E2E_REUSE_SERVER),
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `${stackScript} frontend`,
      url: baseURL,
      reuseExistingServer: !!(process.env.E2E_REUSE_SERVER),
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
