#!/bin/bash
# =============================================================================
# AI Gateway - Production Deployment Script
# =============================================================================
# Usage:
#   ./scripts/deploy.sh              # Deploy all services
#   ./scripts/deploy.sh --build      # Force rebuild images
#   ./scripts/deploy.sh --pull       # Pull latest images
#   ./scripts/deploy.sh --infra      # Deploy infrastructure only
#   ./scripts/deploy.sh --app        # Deploy application only
#   ./scripts/deploy.sh --down       # Stop all services
#   ./scripts/deploy.sh --logs       # Show logs
#   ./scripts/deploy.sh --status     # Show status
# =============================================================================

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Default values
BUILD=false
PULL=false
INFRA_ONLY=false
APP_ONLY=false
SHOW_LOGS=false
SHOW_STATUS=false
STOP=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --build)
            BUILD=true
            shift
            ;;
        --pull)
            PULL=true
            shift
            ;;
        --infra)
            INFRA_ONLY=true
            shift
            ;;
        --app)
            APP_ONLY=true
            shift
            ;;
        --down)
            STOP=true
            shift
            ;;
        --logs)
            SHOW_LOGS=true
            shift
            ;;
        --status)
            SHOW_STATUS=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --build    Force rebuild Docker images"
            echo "  --pull     Pull latest base images"
            echo "  --infra    Deploy infrastructure only (postgres, redis, qdrant)"
            echo "  --app      Deploy application only (gateway, frontend)"
            echo "  --down     Stop all services"
            echo "  --logs     Show logs"
            echo "  --status   Show service status"
            echo "  -h, --help Show this help"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# Functions
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

check_requirements() {
    log_info "Checking requirements..."

    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi

    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_error "Docker Compose is not installed"
        exit 1
    fi

    log_success "Requirements check passed"
}

check_env_file() {
    if [ ! -f ".env" ]; then
        log_warn ".env file not found"
        if [ -f ".env.production" ]; then
            log_info "Copying .env.production to .env"
            cp .env.production .env
        else
            log_error "No .env or .env.production found. Please create one."
            exit 1
        fi
    fi
    log_success "Environment file found"
}

get_compose_cmd() {
    if docker compose version &> /dev/null 2>&1; then
        echo "docker compose"
    else
        echo "docker-compose"
    fi
}

COMPOSE_CMD=$(get_compose_cmd)

# Stop services
if [ "$STOP" = true ]; then
    log_info "Stopping all services..."
    $COMPOSE_CMD down
    log_success "All services stopped"
    exit 0
fi

# Show logs
if [ "$SHOW_LOGS" = true ]; then
    $COMPOSE_CMD logs -f
    exit 0
fi

# Show status
if [ "$SHOW_STATUS" = true ]; then
    # Load .env file for passwords if exists
    if [ -f ".env" ]; then
        set -a
        source .env
        set +a
    fi

    echo ""
    echo "=== Service Status ==="
    $COMPOSE_CMD ps
    echo ""
    echo "=== Health Checks ==="

    # PostgreSQL - use docker exec for reliable check
    echo -n "PostgreSQL: "
    if docker exec ai-gateway-pg pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-gateway}" &>/dev/null; then
        echo "Healthy"
    else
        echo "Not ready or not running"
    fi

    # Redis - use docker exec with password from env
    echo -n "Redis: "
    if docker exec ai-gateway-redis redis-cli -a "${REDIS_PASSWORD:-111111}" ping 2>/dev/null | grep -q PONG; then
        echo "Healthy"
    else
        echo "Not ready or not running"
    fi

    # Qdrant - HTTP health check
    echo -n "Qdrant: "
    if curl -sf http://localhost:${QDRANT_HTTP_PORT:-6333}/healthz &>/dev/null; then
        echo "Healthy"
    else
        echo "Not accessible"
    fi

    # Gateway - HTTP health check
    echo -n "Gateway: "
    if curl -sf http://localhost:${GATEWAY_PORT:-8080}/health &>/dev/null; then
        echo "Healthy"
    else
        echo "Not accessible"
    fi

    # Frontend - HTTP health check
    echo -n "Frontend: "
    if curl -sf http://localhost:${FRONTEND_PORT:-80}/health &>/dev/null; then
        echo "Healthy"
    else
        echo "Not accessible"
    fi

    exit 0
fi

# Main deployment
log_info "Starting AI Gateway deployment..."
echo ""

check_requirements
check_env_file

# Pull images if requested
if [ "$PULL" = true ]; then
    log_info "Pulling latest base images..."
    $COMPOSE_CMD pull
fi

# Define services to deploy
if [ "$INFRA_ONLY" = true ]; then
    SERVICES="postgres redis qdrant"
elif [ "$APP_ONLY" = true ]; then
    SERVICES="gateway frontend"
else
    SERVICES=""
fi

# Build and deploy
if [ "$BUILD" = true ]; then
    log_info "Building Docker images..."
    if [ -n "$SERVICES" ]; then
        $COMPOSE_CMD build $SERVICES
    else
        $COMPOSE_CMD build
    fi
fi

log_info "Starting services..."
if [ -n "$SERVICES" ]; then
    $COMPOSE_CMD up -d $SERVICES
else
    $COMPOSE_CMD up -d
fi

# Wait for services to be healthy
log_info "Waiting for services to be healthy..."
sleep 10

# Show status
echo ""
echo "=== Deployment Status ==="
$COMPOSE_CMD ps

echo ""
log_success "AI Gateway deployment completed!"
echo ""
echo "Access points:"
echo "  - Frontend:  http://localhost:80"
echo "  - Backend:   http://localhost:8080"
echo "  - API Docs:  http://localhost:8080/docs"
echo "  - Health:    http://localhost:8080/health"
echo ""
echo "Commands:"
echo "  - View logs:   $COMPOSE_CMD logs -f"
echo "  - Stop:        $COMPOSE_CMD down"
echo "  - Restart:     $COMPOSE_CMD restart"
echo ""
