/**
 * CLI entry point — argument parsing + Ink App render.
 *
 * Usage:
 *   ai-gateway --api-key gw_xxx --base-url http://localhost:8080
 *   ai-gateway --config   # Open config wizard
 */

import { render } from "ink";
import meow from "meow";
import React from "react";
import { initConfigDir, updateConfig, loadConfig } from "./config.js";
import type { CLIConfig } from "./types/config.js";
import { App } from "./app.js";

const cli = meow(
  `
  Usage
    $ ai-gateway [options]

  Options
    --api-key, -k     API key for the AI Gateway
    --base-url, -u    Gateway base URL (default: http://localhost:8080)
    --model, -m       Default model (default: qwen3.7-plus)
    --tenant-id, -t   Tenant identifier
    --kb              Knowledge base dataset IDs (comma-separated)
    --version, -v     Show version

  Examples
    $ ai-gateway --api-key gw_xxx --base-url http://localhost:8080
    $ ai-gateway -k gw_xxx -m qwen3.7-plus
    $ ai-gateway --kb product-docs,sales
`,
  {
    importMeta: import.meta,
    flags: {
      apiKey: { type: "string", shortFlag: "k" },
      baseUrl: { type: "string", shortFlag: "u" },
      model: { type: "string", shortFlag: "m" },
      tenantId: { type: "string", shortFlag: "t" },
      kb: { type: "string" },
    },
  },
);

// Initialize config directory
const savedConfig = initConfigDir();

// Merge CLI flags > saved config > defaults
const config: CLIConfig = {
  ...savedConfig,
  ...(cli.flags.apiKey && { api_key: cli.flags.apiKey }),
  ...(cli.flags.baseUrl && { base_url: cli.flags.baseUrl }),
  ...(cli.flags.model && { default_model: cli.flags.model }),
  ...(cli.flags.tenantId && { tenant_id: cli.flags.tenantId }),
  ...(cli.flags.kb && {
    kb_dataset_ids: cli.flags.kb.split(",").map((s) => s.trim()),
  }),
};

// Save back if CLI provided new values
if (cli.flags.apiKey || cli.flags.baseUrl || cli.flags.model || cli.flags.tenantId) {
  updateConfig(config);
}

// Check API key
if (!config.api_key) {
  console.error(
    "\x1b[31mError: No API key configured.\x1b[0m\n\n" +
      "Run with: ai-gateway --api-key gw_YOUR_KEY\n" +
      "Or set in: ~/.ai-gateway/config.json\n",
  );
  process.exit(1);
}

// Render the Ink app
render(<App config={config} />);
