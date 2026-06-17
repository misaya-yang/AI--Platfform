#!/bin/bash
# =============================================================================
# AI Gateway - Database Migration (with version tracking)
# =============================================================================
# Tracks which migrations have been applied using a schema_migrations table.
# Safe to run repeatedly — only new migrations are applied.
#
# Usage:  make migrate
#   or:   ./scripts/new/migrate.sh [OPTIONS]
#
# Options:
#   --init       Initialize schema (first-time setup)
#   --status     Show migration status
#   --auto       Non-interactive mode (used by deploy.sh)
#   --env FILE   Use a specific env file instead of .env
# =============================================================================

source "$(dirname "$0")/common.sh"

# -- Parse args --------------------------------------------------------------
INIT=false
STATUS=false
AUTO=false
EXPLICIT_ENV_FILE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --init)   INIT=true; shift ;;
        --status) STATUS=true; shift ;;
        --auto)   AUTO=true; shift ;;
        --env)
            if [ -z "${2:-}" ] || [[ "${2:-}" =~ ^-- ]]; then
                log_error "--env requires a file path"
                exit 2
            fi
            EXPLICIT_ENV_FILE=true
            ENV_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--init] [--status] [--auto] [--env FILE]"
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 2 ;;
    esac
done

if [ "$EXPLICIT_ENV_FILE" = true ]; then
    require_env_file
fi

load_env

cd "$PROJECT_ROOT"

# -- Ensure migration tracking table exists ----------------------------------
ensure_tracking_table() {
    run_sql "
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    " >/dev/null
}

base_schema_exists() {
    run_sql "
        SELECT CASE
            WHEN to_regclass('public.services') IS NULL
              OR to_regclass('public.datasets') IS NULL
              OR to_regclass('public.documents') IS NULL
              OR to_regclass('public.segments') IS NULL
            THEN 'missing'
            ELSE 'present'
        END;
    " 2>/dev/null \
        | grep -q "present"
}

ensure_base_schema() {
    if base_schema_exists; then
        return 0
    fi

    if [ ! -f "database/schema.sql" ]; then
        log_error "database/schema.sql not found"
        exit 1
    fi

    log_info "Base schema missing; applying schema.sql..."
    if ! output=$(run_sql_file "database/schema.sql" 2>&1); then
        log_error "Base schema initialization failed"
        echo "$output" | grep "^ERROR" || true
        exit 1
    fi
    if echo "$output" | grep -q "^ERROR"; then
        log_error "Base schema initialization failed"
        echo "$output" | grep "^ERROR"
        exit 1
    fi
}

# -- Check if migration was already applied ----------------------------------
is_applied() {
    local filename="$1"
    # Sanitize filename: only allow alphanumeric, underscore, hyphen, dot
    if [[ ! "$filename" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        log_error "Invalid migration filename: $filename"
        return 1
    fi
    local result
    result=$(run_sql "SELECT 1 FROM schema_migrations WHERE filename = '${filename}';" 2>/dev/null | grep -c "1" || true)
    [ "$result" -gt 0 ]
}

# -- Record migration as applied ---------------------------------------------
record_migration() {
    local filename="$1"
    if [[ ! "$filename" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        log_error "Invalid migration filename: $filename"
        return 1
    fi
    run_sql "INSERT INTO schema_migrations (filename) VALUES ('${filename}') ON CONFLICT DO NOTHING;" >/dev/null
}

# -- Show status -------------------------------------------------------------
if [ "$STATUS" = true ]; then
    log_step "Migration status"
    ensure_tracking_table

    echo ""
    echo "Applied migrations:"
    run_sql "SELECT filename, applied_at FROM schema_migrations ORDER BY filename;" 2>/dev/null

    echo ""
    echo "Pending migrations:"
    pending=0
    if [ -d "database/migrations" ]; then
        for f in database/migrations/*.sql; do
            [ -f "$f" ] || continue
            basename=$(basename "$f")
            if ! is_applied "$basename"; then
                echo "  - $basename"
                pending=$((pending + 1))
            fi
        done
    fi
    [ "$pending" -eq 0 ] && echo "  (none)"
    exit 0
fi

# -- Initialize schema -------------------------------------------------------
if [ "$INIT" = true ]; then
    log_step "Initializing database schema"

    if [ ! -f "database/schema.sql" ]; then
        log_error "database/schema.sql not found"
        exit 1
    fi

    log_info "Applying schema.sql..."
    if ! output=$(run_sql_file "database/schema.sql" 2>&1); then
        log_error "Schema initialization failed"
        echo "$output" | grep "^ERROR" || true
        exit 1
    fi
    echo "$output" | grep -E "^(CREATE|INSERT|ALTER|NOTICE|ERROR)" | head -30 || true
    log_success "Schema initialized"
    ensure_tracking_table
    exit 0
fi

# -- Run pending migrations --------------------------------------------------
log_step "Running database migrations"
ensure_base_schema
ensure_tracking_table

if [ ! -d "database/migrations" ]; then
    log_info "No migrations directory found"
    exit 0
fi

applied=0
skipped=0

for migration_file in database/migrations/*.sql; do
    [ -f "$migration_file" ] || continue

    filename=$(basename "$migration_file")

    if is_applied "$filename"; then
        skipped=$((skipped + 1))
        continue
    fi

    log_info "Applying: $filename"
    if ! output=$(run_sql_file "$migration_file" 2>&1); then
        log_error "Migration failed: $filename"
        echo "$output" | grep "^ERROR" || true
        if [ "$AUTO" = true ]; then
            log_error "Stopping automatic migration run; fix the migration and rerun."
        fi
        exit 1
    fi

    # Check for errors (but not NOTICEs)
    if echo "$output" | grep -q "^ERROR"; then
        log_error "Migration failed: $filename"
        echo "$output" | grep "^ERROR"
        if [ "$AUTO" = true ]; then
            log_error "Stopping automatic migration run; fix the migration and rerun."
        fi
        exit 1
    fi

    record_migration "$filename"
    applied=$((applied + 1))
done

if [ "$applied" -eq 0 ]; then
    log_success "Database is up to date ($skipped migrations already applied)"
else
    log_success "Applied $applied new migration(s), $skipped already applied"
fi
