# AI Gateway Independent Agent CLI

`ai-gateway` is a standalone local Agent product. It does not use the Web
console or Gateway model configuration. The launcher selects a CLI-owned
provider profile, creates an isolated runtime home, and starts the lock-pinned
composed Rust CLI from `codex-harness`. The Rust App Server remains the only
Agent loop and owns threads, turns, tools, approvals, interruption, compaction,
resume, and interactive/headless rendering.

## Product path

```text
ai-gateway launcher
  -> ~/.ai-gateway-cli/providers.json
  -> composed Rust codex CLI + in-process App Server
  -> provider /v1/responses
```

Providers that expose a real Responses API are called directly. A provider
that only exposes Chat Completions uses a loopback adapter which translates a
strict text/function-tool subset to Responses events. The adapter is a wire
converter, not a second Agent loop.

## Native Runtime binary

Published packages place the composed native binary under
`vendor/<platform>-<arch>/codex`. During development, point the launcher to a
trusted binary built from the pinned source plus this repository's overlay:

```bash
export AI_GATEWAY_AGENT_RUNTIME_BIN=/absolute/path/to/codex
export AI_GATEWAY_AGENT_RUNTIME_RECEIPT=/absolute/path/to/artifact.json
```

For an unreceipted development binary, the additional
`AI_GATEWAY_UNSAFE_DEV_RUNTIME=1` opt-in is required and is never release
evidence.

Explicit binary overrides are development-only even when they carry a receipt.
Release packages execute only the platform artifact assembled under `vendor/`.

The canonical Docker-contained Linux build is:

```bash
AI_PLATFORM_AGENT_RUNTIME_SOURCE=/absolute/clean/codex-harness \
  scripts/harness/build_agent_runtime_cli_package.sh
```

The Docker build installs the Linux artifact and receipt into the matching
`sdk/cli/vendor/linux-<node-arch>/` package layout. The current tag workflow
stages and verifies Linux x64. Darwin and Windows hosted native build jobs are
still release work; the package must not be represented as supporting a target
until its binary and receipt are assembled into the corresponding directory.

Host Cargo is not an accepted build or verification path in this repository.

## Configure

The default product home is `~/.ai-gateway-cli`; override it with
`AI_GATEWAY_CLI_HOME`. It is separate from both `~/.codex` and the legacy
Gateway chat CLI config.

```bash
ai-gateway config init
ai-gateway config path
```

Add a Responses-compatible provider without putting its key on the command
line or in the config file:

```bash
ai-gateway provider add qwen-responses \
  --base-url https://provider.example/openai/v1 \
  --model provider-model-id \
  --wire-api responses \
  --api-key-env QWEN_RESPONSES_API_KEY
export QWEN_RESPONSES_API_KEY='...'
```

For a Chat Completions-only provider:

```bash
ai-gateway provider add qwen-chat \
  --base-url https://provider.example/compatible-mode/v1 \
  --model provider-model-id \
  --wire-api chat-completions \
  --api-key-env QWEN_CHAT_API_KEY
ai-gateway provider use qwen-chat
```

Advanced configuration is stored in `providers.json`:

```json
{
  "schema_version": "ai-gateway-cli/providers/v1",
  "active_provider": "azure-responses",
  "approval_policy": "on-request",
  "sandbox_mode": "workspace-write",
  "providers": {
    "azure-responses": {
      "name": "Azure Responses",
      "model": "deployment-name",
      "base_url": "https://example.openai.azure.com/openai/deployments/deployment-name/v1",
      "wire_api": "responses",
      "auth": {
        "type": "header",
        "header": "api-key",
        "api_key_env": "AZURE_OPENAI_API_KEY"
      },
      "query_params": { "api-version": "2026-01-01-preview" },
      "request_max_retries": 4,
      "stream_max_retries": 5,
      "stream_idle_timeout_ms": 300000
    }
  }
}
```

Static credential headers and secret query parameters are rejected. Only the
public `version`, `x-client`, `x-client-version`, and `x-sdk-version` static
headers are accepted; every provider-specific/credential header uses
`env_http_headers` whose value is an environment-variable name. Query keys and
values are restricted to URL-unreserved characters because the pinned upstream
provider joins them without a second encoding pass.

## Use

Interactive mode and native subcommands are forwarded to the Rust product:

```bash
ai-gateway
ai-gateway exec --json "Summarize this repository"
ai-gateway exec resume --last "Continue"
ai-gateway resume --last
```

Select a provider for one invocation without changing the saved active
provider:

```bash
ai-gateway --cli-provider qwen-responses exec "Run the tests"
```

The pre-1.5 hosted Gateway chat/MCP/knowledge-base client remains available as
an explicit compatibility surface and continues to use its original
`~/.ai-gateway/config.json` file:

```bash
ai-gateway gateway --api-key gw_xxx --base-url https://gateway.example
```

It is not an alternate provider mode for the independent Agent CLI and never
reads `~/.ai-gateway-cli/providers.json`.

## Security and compatibility boundary

- Provider secret values live only in named environment variables. They are
  never written to JSON/TOML, passed in argv, or copied into error output.
- Generated Runtime config excludes key/token/secret/password/credential
  variables from model-generated shell commands.
- Chat-only provider secrets stay in the Node adapter; the Rust child receives
  only an ephemeral loopback credential.
- Non-TLS provider URLs are rejected except explicit loopback development
  profiles.
- The Chat adapter supports text, function tools, tool results, usage,
  reasoning-effort request passthrough, retries before streaming, and strict
  SSE projection. Stream reconnect is intentionally disabled because replay
  could duplicate output or tool intent. Reasoning history/deltas, images,
  hosted tools, and input forms that cannot be represented losslessly fail
  closed.
- The native build and package verifier derive source identity from the Runtime
  source receipt, overlay manifest, and lock; package assembly fails when they
  disagree. The launcher re-checks receipt target/name and binary SHA-256.
- Pointing at an arbitrary binary or system `codex` requires explicit unsafe
  development opt-ins and is never release evidence.

## Development checks

```bash
npm --prefix sdk/cli ci
npm --prefix sdk/cli run typecheck
npm --prefix sdk/cli test
npm --prefix sdk/cli run build
```

These checks prove the launcher/config/adapter layer. Composed Rust behavior,
native packaging, and real provider compatibility require their separate
hosted/Docker/live receipts.
