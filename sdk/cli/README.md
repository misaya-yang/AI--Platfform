# ai-gateway-cli

A Claude Code-like terminal AI assistant powered by AI Gateway.

## Features

- **Streaming Chat** — Real-time SSE streaming with tool call display
- **OS Agent** — Read/write/edit files, search code (glob/grep), execute shell commands
- **MCP Integration** — Connect to Model Context Protocol servers (stdio transport)
- **Knowledge Base** — Bind and query enterprise knowledge bases
- **Sub-Agent Display** — Parallel agent execution visualization
- **Slash Commands** — `/model`, `/kb`, `/mcp`, `/session`, `/config`, `/help`
- **Permission System** — Three-level access control (auto/confirm/dangerous)

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

# AI can use OS tools (with permission prompts)
> Read the package.json and suggest improvements
> Search for all TODO comments in src/
> Run the test suite
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

## OS Agent Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `read_file` | Auto | Read file with line numbers |
| `glob` | Auto | File pattern search |
| `grep` | Auto | Content regex search |
| `list_dir` | Auto | Directory listing |
| `tree` | Auto | Directory tree |
| `write_file` | Confirm | Create/overwrite file |
| `edit_file` | Confirm | Exact string replacement |
| `bash` | Dangerous | Shell command execution |

## Configuration

```
~/.ai-gateway/
├── config.json          # API key, model, base URL
├── permissions.json     # File access rules
├── mcp_servers.json     # MCP server configs
└── sessions/            # Session cache
```

## License

MIT
