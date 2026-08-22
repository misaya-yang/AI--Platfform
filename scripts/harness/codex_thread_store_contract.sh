#!/bin/bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ENV_FILE=${ENV_FILE:-"$PROJECT_ROOT/.env"}
CODEX_HARNESS_FORK=${CODEX_HARNESS_FORK:-}

if [ -z "$CODEX_HARNESS_FORK" ]; then
    echo "ERROR: CODEX_HARNESS_FORK must point to the controlled Codex fork" >&2
    exit 2
fi
if [ ! -f "$CODEX_HARNESS_FORK/codex-rs/Cargo.toml" ]; then
    echo "ERROR: CODEX_HARNESS_FORK does not contain codex-rs/Cargo.toml" >&2
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
    tests/database/test_codex_runtime_thread_store_migration.py

export AI_PLATFORM_RUNTIME_MIGRATION_PATH="$PROJECT_ROOT/database/migrations/089_codex_runtime_thread_store.sql"
cd "$CODEX_HARNESS_FORK/codex-rs"
just test -p ai-platform-agent-runtime
just test -p ai-platform-agent-runtime --run-ignored ignored-only \
    codex_thread_start_round_trips_through_postgres_store
