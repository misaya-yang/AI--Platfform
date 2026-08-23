#!/bin/bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ENV_FILE=${ENV_FILE:-"$PROJECT_ROOT/.env"}
AI_PLATFORM_AGENT_RUNTIME_SOURCE=${AI_PLATFORM_AGENT_RUNTIME_SOURCE:-}

if [ -z "$AI_PLATFORM_AGENT_RUNTIME_SOURCE" ]; then
    echo "ERROR: AI_PLATFORM_AGENT_RUNTIME_SOURCE must point to the controlled Agent fork" >&2
    exit 2
fi
if [ ! -f "$AI_PLATFORM_AGENT_RUNTIME_SOURCE/codex-rs/Cargo.toml" ]; then
    echo "ERROR: AI_PLATFORM_AGENT_RUNTIME_SOURCE does not contain codex-rs/Cargo.toml" >&2
    exit 2
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: ENV_FILE is required for the isolated PostgreSQL contract" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$PROJECT_ROOT"
uv run --all-packages --extra test pytest -q --no-cov \
    tests/database/test_agent_runtime_thread_store_migration.py

export AI_PLATFORM_RUNTIME_MIGRATION_PATH="$PROJECT_ROOT/database/migrations/089_agent_runtime_thread_store.sql"
cd "$AI_PLATFORM_AGENT_RUNTIME_SOURCE/codex-rs"
just test -p ai-platform-agent-runtime
just test -p ai-platform-agent-runtime --run-ignored ignored-only \
    agent_thread_start_round_trips_through_postgres_store
