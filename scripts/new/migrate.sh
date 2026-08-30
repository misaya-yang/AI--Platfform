#!/bin/bash
# =============================================================================
# Compatibility wrapper for the single PostgreSQL migration authority.
# =============================================================================
# This script intentionally contains no psql, SQL-file, legacy-ledger, or lock
# implementation. ``database.authority`` owns discovery, reconciliation,
# locking, transactions, attempts, fingerprints and every schema/ledger write.
#
# Usage:  make migrate
#   or:   ./scripts/new/migrate.sh [OPTIONS]
#
# Options:
#   --init       Initialize an empty database from the frozen baseline
#   --status     Show read-only authority status
#   --auto       Accepted for deploy compatibility; migration stays fail-fast
#   --env FILE   Use a specific env file instead of .env
# =============================================================================

source "$(dirname "$0")/common.sh"

AUTHORITY_COMMAND="migrate"
EXPLICIT_ENV_FILE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --init)
            AUTHORITY_COMMAND="init-fresh"
            shift
            ;;
        --status)
            AUTHORITY_COMMAND="status"
            shift
            ;;
        --auto)
            shift
            ;;
        --env)
            if [ -z "${2:-}" ] || [[ "${2:-}" =~ ^-- ]]; then
                log_error "--env requires a file path"
                exit 2
            fi
            EXPLICIT_ENV_FILE=true
            ENV_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--init] [--status] [--auto] [--env FILE]"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 2
            ;;
    esac
done

if [ "$EXPLICIT_ENV_FILE" = true ]; then
    require_env_file
fi
load_env
cd "$PROJECT_ROOT"

if ! command -v uv >/dev/null 2>&1; then
    log_error "uv is required to run the repository-pinned database authority"
    exit 1
fi

log_step "Database authority: ${AUTHORITY_COMMAND}"
exec uv run --extra database python -m database.authority "$AUTHORITY_COMMAND"
