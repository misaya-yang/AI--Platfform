# ai-gateway-cli

A terminal client for remote streaming chat through AI Gateway.

## Features

- **Remote Streaming Chat** — Send messages to AI Gateway and render SSE responses in real time
- **Server Event Display** — Show remote tool calls and sub-agent activity reported by the gateway
- **MCP Discovery** — Connect to and inspect Model Context Protocol servers (stdio transport)
- **Knowledge Base** — Bind and query enterprise knowledge bases
- **Slash Commands** — `/model`, `/kb`, `/mcp`, `/session`, `/config`, `/help`

## Install

```bash
npm install -g ai-gateway-cli
```

## Quick Start

```bash
# Start with API key
ai-gateway --api-key gw_YOUR_KEY --base-url http://localhost:8080

# Or configure once
ai-gateway --api-key gw_YOUR_KEY --base-url http://localhost:8080
# Config saved to ~/.ai-gateway/config.json — subsequent runs just:
ai-gateway
```

## Usage

```bash
# Chat with AI
> Tell me about this project structure

# Use slash commands
> /model gemini-3-flash-preview
> /kb product-docs, sales
> /session new
> /help

# Send a prompt to the configured remote gateway
> Summarize the onboarding checklist using the product-docs knowledge base
```

## CLI Options

| Flag | Short | Description |
|------|-------|-------------|
| `--api-key` | `-k` | Gateway API key |
| `--base-url` | `-u` | Gateway URL (default: http://localhost:8080) |
| `--model` | `-m` | Default model |
| `--tenant-id` | `-t` | Tenant identifier |
| `--kb` | | Knowledge base IDs (comma-separated) |
| `--version` | `-v` | Show version |
| `--help` | | Show help |

## Configuration

```
~/.ai-gateway/
├── config.json          # API key, model, base URL
├── mcp_servers.json     # MCP server configs
└── sessions/            # Session cache
```

## License

MIT
