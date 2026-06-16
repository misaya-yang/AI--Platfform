#!/bin/bash
# =============================================================================
# AI Gateway - Production Deployment
# =============================================================================
# Unified deployment: build, deploy, migrate, health check — one command.
#
# Usage:  make deploy [ARGS="--build --cn"]
#   or:   ./scripts/new/deploy.sh [OPTIONS]
#
# Options:
#   --build      Rebuild Docker images
#   --cn         Use China mirrors (implies --build)
#   --pull       Pull latest base images before build
#   --infra      Deploy infrastructure only (postgres, redis, qdrant)
#   --app        Deploy application only (gateway, frontend)
#   --no-migrate Skip database migration after deploy
# =============================================================================

source "$(dirname "$0")/common.sh"
load_env

# -- Defaults ----------------------------------------------------------------
BUILD=false
PULL=false
INFRA_ONLY=false
APP_ONLY=false
USE_CN_MIRROR=false
SKIP_MIGRATE=false

# -- Parse args --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --build)      BUILD=true; shift ;;
        --pull)       PULL=true; shift ;;
        --infra)      INFRA_ONLY=true; shift ;;
        --app)        APP_ONLY=true; shift ;;
        --cn|--china) USE_CN_MIRROR=true; BUILD=true; shift ;;
        --no-migrate) SKIP_MIGRATE=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--build] [--cn] [--pull] [--infra] [--app] [--no-migrate]"
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

# -- Pre-flight checks -------------------------------------------------------
log_step "Pre-flight checks"
require_docker
require_env_file
load_env
if [ "$INFRA_ONLY" = true ]; then
    "$SCRIPT_DIR/validate-env.sh" --infra-only --config-only
else
    "$SCRIPT_DIR/validate-env.sh" --config-only
fi

COMPOSE_CMD=$(get_compose_cmd)
cd "$PROJECT_ROOT"

# -- Pull base images --------------------------------------------------------
if [ "$PULL" = true ]; then
    log_step "Pulling latest base images"
    $COMPOSE_CMD --env-file "$PROJECT_ROOT/.env" pull
fi

# -- Determine services to deploy --------------------------------------------
SERVICES=""
if [ "$INFRA_ONLY" = true ]; then
    SERVICES="postgres redis qdrant"
elif [ "$APP_ONLY" = true ]; then
    SERVICES="gateway frontend"
fi

# -- Build if requested ------------------------------------------------------
if [ "$BUILD" = true ]; then
    log_step "Building Docker images"
    BUILD_ARGS=""
    if [ "$USE_CN_MIRROR" = true ]; then
        log_info "Using China mirrors (PyPI + NPM)"
        BUILD_ARGS="--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn --build-arg NPM_REGISTRY=https://registry.npmmirror.com"
    fi
    # shellcheck disable=SC2086
    $COMPOSE_CMD --env-file "$PROJECT_ROOT/.env" build $BUILD_ARGS $SERVICES
fi

# -- Start services ----------------------------------------------------------
log_step "Starting services"
# shellcheck disable=SC2086
$COMPOSE_CMD --env-file "$PROJECT_ROOT/.env" up -d --remove-orphans $SERVICES

# -- Wait for infrastructure to be healthy -----------------------------------
log_step "Health checks"

if [ "$APP_ONLY" != true ]; then
    wait_for_healthy "PostgreSQL" "check_postgres_health" 30
    wait_for_healthy "Redis" "check_redis_health" 30
    wait_for_healthy "Qdrant" "check_qdrant_health" 30 || log_warn "Qdrant may still be starting"
fi

# -- Run database migrations -------------------------------------------------
if [ "$SKIP_MIGRATE" != true ] && [ "$INFRA_ONLY" != true ]; then
    log_step "Running database migrations"
    "$(dirname "$0")/migrate.sh" --auto
fi

# -- Wait for application health ---------------------------------------------
if [ "$INFRA_ONLY" != true ]; then
    wait_for_healthy "Knowledge service" "check_knowledge_health" 60 || log_warn "Knowledge service may still be starting"
    wait_for_healthy "Assistant service" "check_assistant_health" 60 || log_warn "Assistant service may still be starting"
    wait_for_healthy "MCP docgen service" "check_docgen_health" 60 || log_warn "MCP docgen service may still be starting"
    wait_for_healthy "Gateway" "check_gateway_health" 60 || log_warn "Gateway may still be starting"
    wait_for_healthy "Frontend" "check_frontend_health" 30 || log_warn "Frontend may still be starting"
fi

if [ "$INFRA_ONLY" = true ]; then
    "$SCRIPT_DIR/validate-env.sh" --infra-only --runtime
else
    "$SCRIPT_DIR/validate-env.sh" --runtime
fi

# -- Summary -----------------------------------------------------------------
log_step "Deployment complete"
$COMPOSE_CMD ps
echo ""
log_success "AI Gateway is running"
echo ""
echo "  Frontend:  http://localhost:${FRONTEND_PORT:-8081}"
echo "  Backend:   http://localhost:${GATEWAY_PORT:-8080}"
echo "  API Docs:  http://localhost:${GATEWAY_PORT:-8080}/docs"
echo ""
