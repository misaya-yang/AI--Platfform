#!/bin/bash
# =============================================================================
# AI Gateway - Development Environment Setup Script
# =============================================================================
# One-click setup for local development:
#   - Start Docker containers (PostgreSQL, Redis, Qdrant)
#   - Initialize database schema
#   - Run all migrations
#   - Create admin user
#
# Usage:
#   ./scripts/setup-dev.sh              # Full setup
#   ./scripts/setup-dev.sh --start      # Start containers only
#   ./scripts/setup-dev.sh --stop       # Stop containers
#   ./scripts/setup-dev.sh --reset      # Reset everything (destroy + recreate)
#   ./scripts/setup-dev.sh --status     # Show status
#   ./scripts/setup-dev.sh --db-only    # Database init/migration only (containers must be running)
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# =============================================================================
# Configuration (can be overridden by .env file)
# =============================================================================
# Load .env if exists
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# Container names
POSTGRES_CONTAINER="ai-gateway-postgres"
REDIS_CONTAINER="ai-gateway-redis"
QDRANT_CONTAINER="ai-gateway-qdrant"

# PostgreSQL settings (parse from DSN or use defaults)
if [[ "$GATEWAY_DATABASE__DSN" =~ postgresql://([^:]+):([^@]+)@([^:]+):([0-9]+)/(.+) ]]; then
    PG_USER="${BASH_REMATCH[1]}"
    PG_PASSWORD="${BASH_REMATCH[2]}"
    PG_HOST="${BASH_REMATCH[3]}"
    PG_PORT="${BASH_REMATCH[4]}"
    PG_DATABASE="${BASH_REMATCH[5]}"
else
    PG_USER="${POSTGRES_USER:-postgres}"
    PG_PASSWORD="${POSTGRES_PASSWORD:-111111}"
    PG_HOST="${POSTGRES_HOST:-127.0.0.1}"
    PG_PORT="${POSTGRES_PORT:-5433}"
    PG_DATABASE="${POSTGRES_DB:-gateway}"
fi

# Redis settings (parse from URL or use defaults)
if [[ "$GATEWAY_REDIS__URL" =~ redis://:([^@]+)@([^:]+):([0-9]+) ]]; then
    REDIS_PASSWORD="${BASH_REMATCH[1]}"
    REDIS_HOST="${BASH_REMATCH[2]}"
    REDIS_PORT="${BASH_REMATCH[3]}"
else
    REDIS_PASSWORD="${REDIS_PASSWORD:-111111}"
    REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
    REDIS_PORT="${REDIS_PORT:-6379}"
fi

# Qdrant settings
if [[ "$GATEWAY_KNOWLEDGE__QDRANT__URL" =~ http://([^:]+):([0-9]+) ]]; then
    QDRANT_HOST="${BASH_REMATCH[1]}"
    QDRANT_PORT="${BASH_REMATCH[2]}"
else
    QDRANT_HOST="${QDRANT_HOST:-127.0.0.1}"
    QDRANT_PORT="${QDRANT_PORT:-6333}"
fi
QDRANT_GRPC_PORT="${QDRANT_GRPC_PORT:-6334}"

# =============================================================================
# Logging functions
# =============================================================================
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# =============================================================================
# Check requirements
# =============================================================================
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed. Please install Docker first."
        exit 1
    fi

    if ! docker info &> /dev/null; then
        log_error "Docker is not running. Please start Docker Desktop."
        exit 1
    fi

    log_success "Docker is available"
}

# =============================================================================
# Container management
# =============================================================================
container_exists() {
    docker ps -a --format '{{.Names}}' | grep -q "^$1$"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -q "^$1$"
}

start_postgres() {
    log_info "Starting PostgreSQL..."

    if container_running "$POSTGRES_CONTAINER"; then
        log_info "PostgreSQL is already running"
        return 0
    fi

    if container_exists "$POSTGRES_CONTAINER"; then
        docker start "$POSTGRES_CONTAINER"
    else
        docker run -d \
            --name "$POSTGRES_CONTAINER" \
            -e POSTGRES_USER="$PG_USER" \
            -e POSTGRES_PASSWORD="$PG_PASSWORD" \
            -e POSTGRES_DB="$PG_DATABASE" \
            -p "${PG_PORT}:5432" \
            --restart unless-stopped \
            postgres:16-alpine
    fi

    log_success "PostgreSQL started on port $PG_PORT"
}

start_redis() {
    log_info "Starting Redis..."

    if container_running "$REDIS_CONTAINER"; then
        log_info "Redis is already running"
        return 0
    fi

    if container_exists "$REDIS_CONTAINER"; then
        docker start "$REDIS_CONTAINER"
    else
        docker run -d \
            --name "$REDIS_CONTAINER" \
            -p "${REDIS_PORT}:6379" \
            --restart unless-stopped \
            redis:7-alpine \
            redis-server --requirepass "$REDIS_PASSWORD"
    fi

    log_success "Redis started on port $REDIS_PORT"
}

start_qdrant() {
    log_info "Starting Qdrant..."

    if container_running "$QDRANT_CONTAINER"; then
        log_info "Qdrant is already running"
        return 0
    fi

    if container_exists "$QDRANT_CONTAINER"; then
        docker start "$QDRANT_CONTAINER"
    else
        docker run -d \
            --name "$QDRANT_CONTAINER" \
            -p "${QDRANT_PORT}:6333" \
            -p "${QDRANT_GRPC_PORT}:6334" \
            --restart unless-stopped \
            qdrant/qdrant:latest
    fi

    log_success "Qdrant started on port $QDRANT_PORT"
}

stop_containers() {
    log_info "Stopping containers..."

    for container in "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" "$QDRANT_CONTAINER"; do
        if container_running "$container"; then
            docker stop "$container"
            log_info "Stopped $container"
        fi
    done

    log_success "All containers stopped"
}

remove_containers() {
    log_info "Removing containers..."

    for container in "$POSTGRES_CONTAINER" "$REDIS_CONTAINER" "$QDRANT_CONTAINER"; do
        if container_exists "$container"; then
            docker rm -f "$container" 2>/dev/null || true
            log_info "Removed $container"
        fi
    done

    log_success "All containers removed"
}

wait_for_postgres() {
    log_info "Waiting for PostgreSQL to be ready..."

    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if docker exec "$POSTGRES_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DATABASE" &>/dev/null; then
            log_success "PostgreSQL is ready"
            return 0
        fi
        echo -n "."
        sleep 1
        attempt=$((attempt + 1))
    done

    echo ""
    log_error "PostgreSQL failed to start within ${max_attempts}s"
    exit 1
}

wait_for_redis() {
    log_info "Waiting for Redis to be ready..."

    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if docker exec "$REDIS_CONTAINER" redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q PONG; then
            log_success "Redis is ready"
            return 0
        fi
        echo -n "."
        sleep 1
        attempt=$((attempt + 1))
    done

    echo ""
    log_error "Redis failed to start within ${max_attempts}s"
    exit 1
}

wait_for_qdrant() {
    log_info "Waiting for Qdrant to be ready..."

    local max_attempts=30
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if curl -sf "http://${QDRANT_HOST}:${QDRANT_PORT}/healthz" &>/dev/null; then
            log_success "Qdrant is ready"
            return 0
        fi
        echo -n "."
        sleep 1
        attempt=$((attempt + 1))
    done

    echo ""
    log_warn "Qdrant health check failed (may still be starting)"
}

# =============================================================================
# Database operations
# =============================================================================
run_sql() {
    docker exec -i "$POSTGRES_CONTAINER" psql -U "$PG_USER" -d "$PG_DATABASE" "$@"
}

init_database() {
    log_step "Initializing Database Schema"

    if [ ! -f "database/schema.sql" ]; then
        log_error "database/schema.sql not found"
        exit 1
    fi

    log_info "Running schema.sql..."
    run_sql < database/schema.sql 2>&1 | grep -E "^(CREATE|INSERT|ALTER|DROP|NOTICE|ERROR)" | head -50

    log_success "Database schema initialized"
}

run_migrations() {
    log_step "Running Migrations"

    if [ ! -d "database/migrations" ]; then
        log_warn "No migrations directory found"
        return 0
    fi

    local migration_count=0

    for migration in database/migrations/*.sql; do
        if [ -f "$migration" ]; then
            log_info "Running: $(basename "$migration")"
            run_sql < "$migration" 2>&1 | grep -E "^(CREATE|INSERT|UPDATE|ALTER|DROP|NOTICE|ERROR)" | head -20
            migration_count=$((migration_count + 1))
        fi
    done

    log_success "Completed $migration_count migrations"
}

verify_admin() {
    log_step "Verifying Admin Account"

    local admin_check=$(run_sql -t -c "SELECT user_id, email, status FROM users WHERE user_id = 'admin';")

    if echo "$admin_check" | grep -q "admin"; then
        log_success "Admin account verified"
        echo ""
        echo -e "  ${GREEN}Admin Login:${NC}"
        echo -e "    Email:    ${CYAN}admin@hejazfs.com.au${NC}"
        echo -e "    Password: ${CYAN}123456.dc${NC}"
    else
        log_warn "Admin account not found. You may need to create one manually."
    fi
}

show_status() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  AI Gateway Development Environment Status${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
    echo ""

    echo "Container Status:"
    echo "─────────────────────────────────────────────"
    printf "  %-20s %s\n" "PostgreSQL:" "$(container_running $POSTGRES_CONTAINER && echo -e "${GREEN}Running${NC}" || echo -e "${RED}Stopped${NC}")"
    printf "  %-20s %s\n" "Redis:" "$(container_running $REDIS_CONTAINER && echo -e "${GREEN}Running${NC}" || echo -e "${RED}Stopped${NC}")"
    printf "  %-20s %s\n" "Qdrant:" "$(container_running $QDRANT_CONTAINER && echo -e "${GREEN}Running${NC}" || echo -e "${RED}Stopped${NC}")"

    echo ""
    echo "Connection Info:"
    echo "─────────────────────────────────────────────"
    echo "  PostgreSQL: postgresql://${PG_USER}:****@${PG_HOST}:${PG_PORT}/${PG_DATABASE}"
    echo "  Redis:      redis://:****@${REDIS_HOST}:${REDIS_PORT}"
    echo "  Qdrant:     http://${QDRANT_HOST}:${QDRANT_PORT}"

    if container_running "$POSTGRES_CONTAINER"; then
        echo ""
        echo "Database Tables:"
        echo "─────────────────────────────────────────────"
        run_sql -c "\dt" 2>/dev/null | head -30 || echo "  (unable to list tables)"
    fi

    echo ""
}

# =============================================================================
# Main
# =============================================================================
print_banner() {
    echo ""
    echo -e "${CYAN}╔═══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║       AI Gateway - Development Environment Setup          ║${NC}"
    echo -e "${CYAN}╚═══════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

print_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  (no args)    Full setup: start containers + init database + migrations"
    echo "  --start      Start Docker containers only"
    echo "  --stop       Stop all containers"
    echo "  --reset      Reset everything (remove containers and recreate)"
    echo "  --status     Show current status"
    echo "  --db-only    Run database init and migrations only (containers must be running)"
    echo "  -h, --help   Show this help"
    echo ""
    echo "Examples:"
    echo "  ./scripts/setup-dev.sh              # First time setup"
    echo "  ./scripts/setup-dev.sh --start      # Start containers after reboot"
    echo "  ./scripts/setup-dev.sh --reset      # Reset and recreate everything"
    echo ""
}

# Parse arguments
ACTION="full"

while [[ $# -gt 0 ]]; do
    case $1 in
        --start)
            ACTION="start"
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --reset)
            ACTION="reset"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --db-only)
            ACTION="db-only"
            shift
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            print_help
            exit 1
            ;;
    esac
done

# Execute action
print_banner
check_docker

case $ACTION in
    "stop")
        stop_containers
        ;;
    "status")
        show_status
        ;;
    "start")
        log_step "Starting Containers"
        start_postgres
        start_redis
        start_qdrant
        wait_for_postgres
        wait_for_redis
        wait_for_qdrant
        show_status
        ;;
    "reset")
        log_step "Resetting Environment"
        stop_containers
        remove_containers
        log_step "Starting Fresh Containers"
        start_postgres
        start_redis
        start_qdrant
        wait_for_postgres
        wait_for_redis
        wait_for_qdrant
        init_database
        run_migrations
        verify_admin
        show_status
        ;;
    "db-only")
        wait_for_postgres
        init_database
        run_migrations
        verify_admin
        ;;
    "full")
        log_step "Starting Containers"
        start_postgres
        start_redis
        start_qdrant
        wait_for_postgres
        wait_for_redis
        wait_for_qdrant
        init_database
        run_migrations
        verify_admin
        show_status
        log_success "Development environment is ready!"
        echo ""
        echo "Next steps:"
        echo "  1. Start the backend:  python -m uvicorn src.main:app --reload --port 8080"
        echo "  2. Start the frontend: cd frontend && npm run dev"
        echo ""
        ;;
esac
