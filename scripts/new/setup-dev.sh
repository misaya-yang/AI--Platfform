#!/bin/bash
# =============================================================================
# AI Gateway - Development Environment Setup
# =============================================================================
# Usage:  make dev-setup    / make dev-start / make dev-stop / make dev-reset
#   or:   ./scripts/new/setup-dev.sh [OPTIONS]
#
# Options:
#   (default)    Full setup: containers + schema + migrations
#   --start      Start containers only
#   --stop       Stop containers
#   --reset      Destroy and recreate everything
#   --status     Show current status
#   --env FILE   Use a specific env file instead of .env
# =============================================================================

source "$(dirname "$0")/common.sh"

# -- Parse args --------------------------------------------------------------
ACTION="full"
EXPLICIT_ENV_FILE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --start)  ACTION="start"; shift ;;
        --stop)   ACTION="stop"; shift ;;
        --reset)  ACTION="reset"; shift ;;
        --status) ACTION="status"; shift ;;
        --env)
            if [ -z "${2:-}" ] || [[ "${2:-}" =~ ^-- ]]; then
                log_error "--env requires a file path"
                exit 2
            fi
            EXPLICIT_ENV_FILE=true
            ENV_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--start|--stop|--reset|--status] [--env FILE]"
            exit 0 ;;
        *) log_error "Unknown: $1"; exit 1 ;;
    esac
done

if [ "$EXPLICIT_ENV_FILE" = true ]; then
    require_env_file
fi

load_env

# -- Dev container config (standalone, not via docker-compose) ----------------
DEV_PG_CONTAINER="${POSTGRES_CONTAINER:-ai-gateway-postgres}"
DEV_REDIS_CONTAINER="${REDIS_CONTAINER:-ai-gateway-redis}"
DEV_QDRANT_CONTAINER="${QDRANT_CONTAINER:-ai-gateway-qdrant}"

# Parse DSN if available, otherwise use env vars
if [[ "${GATEWAY_DATABASE__DSN:-}" =~ postgresql://([^:]+):([^@]+)@([^:]+):([0-9]+)/(.+) ]]; then
    PG_USER="${BASH_REMATCH[1]}"
    PG_PASS="${BASH_REMATCH[2]}"
    PG_HOST="${BASH_REMATCH[3]}"
    PG_PORT="${BASH_REMATCH[4]}"
    PG_DB="${BASH_REMATCH[5]}"
else
    PG_USER="${POSTGRES_USER:-postgres}"
    PG_PASS="${POSTGRES_PASSWORD:-}"
    PG_HOST="${POSTGRES_HOST:-127.0.0.1}"
    PG_PORT="${POSTGRES_PORT:-5433}"
    PG_DB="${POSTGRES_DB:-gateway}"
fi

REDIS_PASS="${REDIS_PASSWORD:-}"
REDIS_DEV_PORT="${REDIS_PORT:-6379}"
QDRANT_DEV_PORT="${QDRANT_HTTP_PORT:-${QDRANT_PORT:-6333}}"
QDRANT_GRPC_DEV_PORT="${QDRANT_GRPC_PORT:-6334}"

require_dev_runtime_config() {
    local missing=false

    if [ -z "$PG_PASS" ]; then
        log_error "POSTGRES_PASSWORD is required"
        missing=true
    fi
    if [ -z "$REDIS_PASS" ]; then
        log_error "REDIS_PASSWORD is required"
        missing=true
    fi

    [ "$missing" = false ] || exit 1
}

# -- Container helpers -------------------------------------------------------
container_running() { docker ps --format '{{.Names}}' | grep -q "^$1$"; }
container_exists()  { docker ps -a --format '{{.Names}}' | grep -q "^$1$"; }

ensure_container() {
    local name="$1"; shift
    local image="$1"; shift
    # Remaining args are docker run flags, optionally followed by "--" and
    # the container command. Docker requires the image before the command.
    local run_args=()
    local command_args=()
    local after_separator=false
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--" ] && [ "$after_separator" = false ]; then
            after_separator=true
            shift
            continue
        fi
        if [ "$after_separator" = true ]; then
            command_args+=("$1")
        else
            run_args+=("$1")
        fi
        shift
    done

    if container_running "$name"; then
        log_info "$name already running"
        return 0
    fi

    if container_exists "$name"; then
        docker start "$name" >/dev/null
    else
        if [ "${#command_args[@]}" -gt 0 ]; then
            docker run -d --name "$name" --restart unless-stopped "${run_args[@]}" "$image" "${command_args[@]}" >/dev/null
        else
            docker run -d --name "$name" --restart unless-stopped "${run_args[@]}" "$image" >/dev/null
        fi
    fi
    log_success "$name started"
}

stop_all() {
    for c in "$DEV_PG_CONTAINER" "$DEV_REDIS_CONTAINER" "$DEV_QDRANT_CONTAINER"; do
        container_running "$c" && docker stop "$c" >/dev/null && log_info "Stopped $c"
    done
}

remove_all() {
    for c in "$DEV_PG_CONTAINER" "$DEV_REDIS_CONTAINER" "$DEV_QDRANT_CONTAINER"; do
        container_exists "$c" && docker rm -f "$c" >/dev/null && log_info "Removed $c"
    done
}

start_containers() {
    require_dev_runtime_config

    log_step "Starting dev containers"

    ensure_container "$DEV_PG_CONTAINER" "postgres:16-alpine" \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASS" \
        -e POSTGRES_DB="$PG_DB" \
        -p "${PG_PORT}:5432"

    ensure_container "$DEV_REDIS_CONTAINER" "redis:7-alpine" \
        -p "${REDIS_DEV_PORT}:6379" \
        -- redis-server --requirepass "$REDIS_PASS"

    ensure_container "$DEV_QDRANT_CONTAINER" "qdrant/qdrant:latest" \
        -p "${QDRANT_DEV_PORT}:6333" \
        -p "${QDRANT_GRPC_DEV_PORT}:6334"
}

wait_all_healthy() {
    wait_for_healthy "PostgreSQL" "docker exec $DEV_PG_CONTAINER pg_isready -U $PG_USER -d $PG_DB" 30
    wait_for_healthy "Redis" "docker exec $DEV_REDIS_CONTAINER redis-cli -a $REDIS_PASS ping 2>/dev/null | grep -q PONG" 30
    wait_for_healthy "Qdrant" "curl -sf http://localhost:${QDRANT_DEV_PORT}/healthz" 30 || log_warn "Qdrant may still be starting"
}

init_db() {
    log_step "Database setup"

    if [ -f "${PROJECT_ROOT}/database/schema.sql" ]; then
        log_info "Applying schema.sql..."
        local schema_output
        if ! schema_output="$(
            docker exec -i "$DEV_PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" \
                < "${PROJECT_ROOT}/database/schema.sql" 2>&1
        )"; then
            printf '%s\n' "$schema_output" | grep -E "^(CREATE|INSERT|ALTER|NOTICE|ERROR)" | head -30 || true
            return 1
        fi
        printf '%s\n' "$schema_output" | grep -E "^(CREATE|INSERT|ALTER|NOTICE|ERROR)" | head -30 || true
    fi

    # Run migrations with tracking
    log_info "Running migrations..."
    # Pass dev container name via environment so migrate.sh's common.sh picks it up
    POSTGRES_CONTAINER="$DEV_PG_CONTAINER" ENV_FILE="$ENV_FILE" "$(dirname "$0")/migrate.sh" --auto 2>/dev/null \
        || POSTGRES_CONTAINER="$DEV_PG_CONTAINER" ENV_FILE="$ENV_FILE" "$(dirname "$0")/migrate.sh"
}

show_status() {
    echo ""
    echo "Container Status:"
    for c in "$DEV_PG_CONTAINER" "$DEV_REDIS_CONTAINER" "$DEV_QDRANT_CONTAINER"; do
        local status
        container_running "$c" && status="${GREEN}Running${NC}" || status="${RED}Stopped${NC}"
        printf "  %-25s %b\n" "$c" "$status"
    done

    echo ""
    echo "Connection Info:"
    echo "  PostgreSQL: postgresql://${PG_USER}:****@${PG_HOST}:${PG_PORT}/${PG_DB}"
    echo "  Redis:      redis://:****@127.0.0.1:${REDIS_DEV_PORT}"
    echo "  Qdrant:     http://127.0.0.1:${QDRANT_DEV_PORT}"
    echo ""
}

# -- Execute action ----------------------------------------------------------
require_docker

case $ACTION in
    stop)
        stop_all
        log_success "All containers stopped"
        ;;
    status)
        show_status
        ;;
    start)
        start_containers
        wait_all_healthy
        show_status
        ;;
    reset)
        log_step "Resetting dev environment"
        stop_all
        remove_all
        start_containers
        wait_all_healthy
        init_db
        sync_workspace_packages
        show_status
        log_success "Dev environment reset complete"
        ;;
    full)
        start_containers
        wait_all_healthy
        init_db
        sync_workspace_packages
        show_status
        log_success "Dev environment ready!"
        echo "Next steps:"
        echo "  1. Backend:  python -m uvicorn src.main:app --reload --port 8080"
        echo "  2. Frontend: cd web && npm run dev"
        echo ""
        ;;
esac
