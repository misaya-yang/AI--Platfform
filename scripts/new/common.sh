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
DEFAULT_ENV_FILE="${PROJECT_ROOT}/.env"
ENV_FILE="${ENV_FILE:-$DEFAULT_ENV_FILE}"

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
env_file_path() {
    echo "${ENV_FILE:-$DEFAULT_ENV_FILE}"
}

load_env() {
    local env_file
    env_file="$(env_file_path)"
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
                value="${value%$'\r'}"
                if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
                    value="${value:1:${#value}-2}"
                fi
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
    local env_file
    env_file="$(env_file_path)"
    if [ ! -f "$env_file" ]; then
        log_error "Env file not found: $env_file. Copy .env.example to .env or set ENV_FILE to a populated env file."
        exit 1
    fi
}

# -- Database config from environment ----------------------------------------
# Reads PG config from env vars (loaded via load_env)
pg_host()     { echo "${POSTGRES_HOST:-localhost}"; }
pg_port()     { echo "${POSTGRES_PORT:-5432}"; }
pg_user()     { echo "${POSTGRES_USER:-postgres}"; }
pg_password() { echo "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"; }
pg_database() { echo "${POSTGRES_DB:-gateway}"; }

# Container names (overridable via env vars for dev setup)
pg_container()     { echo "${POSTGRES_CONTAINER:-ai-gateway-pg}"; }
redis_container()  { echo "${REDIS_CONTAINER:-ai-gateway-redis}"; }
qdrant_container() { echo "${QDRANT_CONTAINER:-ai-gateway-qdrant}"; }
assistant_container() { echo "${ASSISTANT_CONTAINER:-ai-gateway-assistant-service}"; }
knowledge_container() { echo "${KNOWLEDGE_CONTAINER:-ai-gateway-knowledge-service}"; }
knowledge_worker_container() { echo "${KNOWLEDGE_WORKER_CONTAINER:-ai-gateway-knowledge-worker}"; }
gateway_container()   { echo "${GATEWAY_CONTAINER:-ai-gateway-backend}"; }
frontend_container()  { echo "${FRONTEND_CONTAINER:-ai-gateway-frontend}"; }
agent_runtime_container() { echo "${AGENT_RUNTIME_CONTAINER:-ai-gateway-agent-runtime}"; }

agent_runtime_kernel_revision() {
    python3 - "$PROJECT_ROOT/deploy/agent-runtime-source/source-receipt.json" <<'PY'
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
source = receipt["source"]["upstream_sha"]
overlay = receipt["overlay"]["sha256"]
print(f"{source}+{overlay[:12]}")
PY
}

agent_runtime_image_tag() {
    python3 - "$PROJECT_ROOT/deploy/agent-runtime-source/source-receipt.json" <<'PY'
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
upstream = receipt["source"]["upstream_sha"]
overlay = receipt["overlay"]["sha256"]
print(f"ai-gateway-agent-runtime:local-{upstream[:12]}-{overlay[:12]}")
PY
}

assert_agent_runtime_image_locked() {
    local task_image="$1"
    if ! docker image inspect "$task_image" >/dev/null 2>&1; then
        log_error "Configured Agent Runtime image is not available locally: $task_image"
        return 1
    fi
    if ! python3 "$PROJECT_ROOT/scripts/harness/agent_runtime_supply_chain.py" validate \
        --repo-root "$PROJECT_ROOT" \
        --lock "$PROJECT_ROOT/deploy/agent-runtime-source/lock.json" \
        --require-artifact agent_runtime >/dev/null; then
        log_error "Agent Runtime source/image lock validation failed"
        return 1
    fi

    local task_locked_digest
    local task_image_digest
    task_locked_digest="$(python3 - "$PROJECT_ROOT/deploy/agent-runtime-source/lock.json" <<'PY'
import json
import sys

lock = json.load(open(sys.argv[1], encoding="utf-8"))
print(lock["oci"]["artifacts"]["agent_runtime"]["image_digest"] or "")
PY
)"
    task_image_digest="$(docker image inspect "$task_image" --format '{{.Id}}')"
    if [ -z "$task_locked_digest" ] || [ "$task_image_digest" != "$task_locked_digest" ]; then
        log_error "Configured Agent Runtime image digest does not match the locked artifact"
        return 1
    fi
    return 0
}

# -- Compose ownership guard -------------------------------------------------
assert_compose_owner() {
    local expected_owner="${1:-$PROJECT_ROOT}"
    local inspected_names=(
        "$(pg_container)"
        "$(redis_container)"
        "$(qdrant_container)"
        "$(gateway_container)"
        "$(frontend_container)"
        "$(assistant_container)"
        "$(knowledge_container)"
        "$(knowledge_worker_container)"
        "$(agent_runtime_container)"
        # Legacy/other-checkout names that have caused local stack confusion.
        ai-gateway-mcp-docgen-server
        assistant-service
        ai-gateway-knowledge
        mcp-docgen-server
        islamic-content-service
    )
    local container owner project service mismatch=false

    for container in "${inspected_names[@]}"; do
        if ! docker inspect "$container" >/dev/null 2>&1; then
            continue
        fi
        owner=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container" 2>/dev/null || true)
        project=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container" 2>/dev/null || true)
        service=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$container" 2>/dev/null || true)
        if [ -z "$owner" ]; then
            log_error "Container '$container' (service=${service:-unknown}) has no Compose working-directory label"
            mismatch=true
        elif [ "$owner" != "$expected_owner" ]; then
            log_error "Container '$container' (service=${service:-unknown}) belongs to a different checkout: $owner"
            mismatch=true
        elif [ "$project" != "ai-gateway" ]; then
            log_error "Container '$container' (service=${service:-unknown}) belongs to a different Compose project: ${project:-unlabeled}"
            mismatch=true
        fi
    done

    if [ "$mismatch" = true ]; then
        log_error "Refusing to mutate Docker project 'ai-gateway' from $PROJECT_ROOT until wrong-checkout containers are stopped or removed explicitly."
        exit 1
    fi
}

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
        docker exec -i "$(pg_container)" psql -v ON_ERROR_STOP=1 -U "$(pg_user)" -d "$(pg_database)" < "$file" 2>&1
    elif command -v psql &>/dev/null; then
        PGPASSWORD="$(pg_password)" psql -v ON_ERROR_STOP=1 -h "$(pg_host)" -p "$(pg_port)" -U "$(pg_user)" -d "$(pg_database)" < "$file" 2>&1
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
    docker exec -e "PGPASSWORD=$(pg_password)" "$(pg_container)" \
        psql -h 127.0.0.1 -U "$(pg_user)" -d "$(pg_database)" -tAc "SELECT 1" 2>/dev/null \
        | grep -q 1
}

check_redis_health() {
    docker exec "$(redis_container)" sh -c \
        'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping' 2>/dev/null | grep -q PONG
}

check_qdrant_health() {
    curl -sf "http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz" &>/dev/null
}

check_gateway_health() {
    curl -sf "http://localhost:${GATEWAY_PORT:-8080}/health/ready" &>/dev/null
}

check_gateway_metrics() {
    local body
    local status
    local url="http://localhost:${GATEWAY_PORT:-8080}/metrics"

    body="$(curl -fsS "$url" 2>/dev/null)" && {
        printf '%s\n' "$body" | grep -Eq '^# HELP |^# TYPE ' || return 1
        printf '%s\n' "$body" | grep -Eq '^gateway_up($|[ {])'
        return
    }

    # Hardened deployments require auth for the Prometheus scrape endpoint.
    # Treat 401/403 as reachable-but-protected; connection errors and 5xx fail.
    status="$(curl -sS -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || true)"
    if [ "$status" = "401" ] || [ "$status" = "403" ]; then
        return 0
    fi

    return 1
}

check_frontend_health() {
    curl -sf "http://localhost:${FRONTEND_PORT:-8081}/health" &>/dev/null
}

check_knowledge_health() {
    docker exec "$(knowledge_container)" curl -sf "http://127.0.0.1:8092/health/ready" &>/dev/null
}

check_knowledge_worker_health() {
    docker exec "$(knowledge_worker_container)" curl -sf "http://127.0.0.1:8092/health/ready" &>/dev/null
}

check_agent_runtime_health() {
    docker exec "$(agent_runtime_container)" curl -sf "http://127.0.0.1:8094/health/ready" &>/dev/null
}

check_assistant_health() {
    docker exec "$(assistant_container)" curl -sf "http://127.0.0.1:8093/health/ready" &>/dev/null
}

check_docgen_health() {
    docker exec "$(assistant_container)" python -c \
        'from pathlib import Path; assert any("mcp_docgen_server" in p.read_bytes().decode("utf-8", "ignore") for p in Path("/proc").glob("[0-9]*/cmdline"))' \
        &>/dev/null
}

# -- Python workspace packages -----------------------------------------------
sync_workspace_packages() {
    log_step "Syncing workspace Python packages"

    if command -v uv >/dev/null 2>&1; then
        (
            cd "$PROJECT_ROOT"
            uv sync --quiet
        ) && log_success "Workspace packages synced via uv" && return 0
    fi

    local pip_cmd=""
    if command -v pip >/dev/null 2>&1; then
        pip_cmd="pip"
    elif command -v pip3 >/dev/null 2>&1; then
        pip_cmd="pip3"
    fi

    if [ -n "$pip_cmd" ]; then
        (
            cd "$PROJECT_ROOT"
            "$pip_cmd" install -e packages/ai-gateway-core -e apps/assistant-service -q
        ) && log_success "Workspace packages synced via editable pip installs" && return 0
    fi

    log_warn "uv/pip not found; skipped workspace package sync"
    return 0
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
