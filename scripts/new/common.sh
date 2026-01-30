#!/bin/bash
# =============================================================================
# AI Gateway - Shared Functions
# =============================================================================
# Source this file in other scripts: source "$(dirname "$0")/common.sh"
# =============================================================================

set -euo pipefail

# -- Project paths -----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# -- Colors ------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# -- Logging -----------------------------------------------------------------
log_info()    { echo -e "${BLUE}[INFO]${NC}    $1"; }
log_success() { echo -e "${GREEN}[OK]${NC}      $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}    $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC}   $1"; }
log_step()    { echo -e "\n${CYAN}${BOLD}=> $1${NC}"; }

# -- Environment loading -----------------------------------------------------
load_env() {
    local env_file="${PROJECT_ROOT}/.env"
    if [ -f "$env_file" ]; then
        set -a
        # Parse KEY=VALUE lines only (skip comments, empty lines; no arbitrary execution)
        while IFS='=' read -r key value; do
            # Skip comments and empty lines
            [[ "$key" =~ ^[[:space:]]*# ]] && continue
            [[ -z "$key" ]] && continue
            # Strip leading/trailing whitespace from key
            key=$(echo "$key" | xargs)
            # Only export valid variable names
            if [[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
                export "$key=$value"
            fi
        done < "$env_file"
        set +a
    fi
}

# -- Docker compose command detection ----------------------------------------
get_compose_cmd() {
    if docker compose version &>/dev/null; then
        echo "docker compose"
    elif command -v docker-compose &>/dev/null; then
        echo "docker-compose"
    else
        log_error "Docker Compose not found"
        exit 1
    fi
}

# -- Requirement checks ------------------------------------------------------
require_docker() {
    if ! command -v docker &>/dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    if ! docker info &>/dev/null; then
        log_error "Docker daemon is not running"
        exit 1
    fi
}

require_env_file() {
    if [ ! -f "${PROJECT_ROOT}/.env" ]; then
        if [ -f "${PROJECT_ROOT}/.env.production" ]; then
            log_warn ".env not found, copying from .env.production"
            cp "${PROJECT_ROOT}/.env.production" "${PROJECT_ROOT}/.env"
        elif [ -f "${PROJECT_ROOT}/.env.example" ]; then
            log_error ".env not found. Create one from .env.example:"
            echo "  cp .env.example .env"
            exit 1
        else
            log_error ".env file not found"
            exit 1
        fi
    fi
}

# -- Database config from environment ----------------------------------------
# Reads PG config from env vars (loaded via load_env)
pg_host()     { echo "${POSTGRES_HOST:-localhost}"; }
pg_port()     { echo "${POSTGRES_PORT:-5432}"; }
pg_user()     { echo "${POSTGRES_USER:-postgres}"; }
pg_password() { echo "${POSTGRES_PASSWORD:-111111}"; }
pg_database() { echo "${POSTGRES_DB:-gateway}"; }

# Container names (overridable via env vars for dev setup)
pg_container()     { echo "${POSTGRES_CONTAINER:-ai-gateway-pg}"; }
redis_container()  { echo "${REDIS_CONTAINER:-ai-gateway-redis}"; }
qdrant_container() { echo "${QDRANT_CONTAINER:-ai-gateway-qdrant}"; }

# -- SQL execution helpers ---------------------------------------------------
# Run SQL via docker exec (production) or psql (dev)
run_sql() {
    local sql="$1"
    if docker ps --format '{{.Names}}' | grep -q "^$(pg_container)$" 2>/dev/null; then
        docker exec -i "$(pg_container)" psql -U "$(pg_user)" -d "$(pg_database)" -c "$sql" 2>/dev/null
    elif command -v psql &>/dev/null; then
        PGPASSWORD="$(pg_password)" psql -h "$(pg_host)" -p "$(pg_port)" -U "$(pg_user)" -d "$(pg_database)" -c "$sql" 2>/dev/null
    else
        log_error "Cannot connect to PostgreSQL (no docker container or psql found)"
        return 1
    fi
}

run_sql_file() {
    local file="$1"
    if docker ps --format '{{.Names}}' | grep -q "^$(pg_container)$" 2>/dev/null; then
        docker exec -i "$(pg_container)" psql -U "$(pg_user)" -d "$(pg_database)" < "$file" 2>&1
    elif command -v psql &>/dev/null; then
        PGPASSWORD="$(pg_password)" psql -h "$(pg_host)" -p "$(pg_port)" -U "$(pg_user)" -d "$(pg_database)" < "$file" 2>&1
    else
        log_error "Cannot connect to PostgreSQL"
        return 1
    fi
}

# -- Health check helpers ----------------------------------------------------
wait_for_healthy() {
    local name="$1"
    local check_cmd="$2"
    local max_attempts="${3:-30}"
    local attempt=1

    log_info "Waiting for ${name}..."
    while [ $attempt -le "$max_attempts" ]; do
        if eval "$check_cmd" &>/dev/null; then
            log_success "${name} is ready"
            return 0
        fi
        printf "."
        sleep 1
        attempt=$((attempt + 1))
    done
    echo ""
    log_error "${name} not ready after ${max_attempts}s"
    return 1
}

check_postgres_health() {
    docker exec "$(pg_container)" pg_isready -U "$(pg_user)" -d "$(pg_database)" &>/dev/null
}

check_redis_health() {
    docker exec "$(redis_container)" redis-cli -a "${REDIS_PASSWORD:-111111}" ping 2>/dev/null | grep -q PONG
}

check_qdrant_health() {
    curl -sf "http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz" &>/dev/null
}

check_gateway_health() {
    curl -sf "http://localhost:${GATEWAY_PORT:-8080}/health" &>/dev/null
}

check_frontend_health() {
    curl -sf "http://localhost:${FRONTEND_PORT:-80}/health" &>/dev/null
}

# -- Confirmation prompt -----------------------------------------------------
confirm() {
    local msg="${1:-Continue?}"
    if [[ "${AUTO_CONFIRM:-}" == "true" ]]; then
        return 0
    fi
    read -p "$msg [y/N] " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]]
}
