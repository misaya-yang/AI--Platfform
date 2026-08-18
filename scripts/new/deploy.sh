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
#   --pull       Pull configured versioned images before deploy/build
#   --infra      Deploy infrastructure only (postgres, redis, qdrant)
#   --app        Deploy application services only (gateway, frontend, assistant,
#                knowledge API/worker, docgen)
#   --no-migrate Skip database migration after deploy
#   --env FILE   Use a specific env file instead of .env
# =============================================================================

source "$(dirname "$0")/common.sh"

# -- Defaults ----------------------------------------------------------------
BUILD=false
PULL=false
INFRA_ONLY=false
APP_ONLY=false
USE_CN_MIRROR=false
SKIP_MIGRATE=false
SHOULD_MIGRATE=false
EXPLICIT_ENV_FILE=false

# -- Parse args --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case $1 in
        --build)      BUILD=true; shift ;;
        --pull)       PULL=true; shift ;;
        --infra)      INFRA_ONLY=true; shift ;;
        --app)        APP_ONLY=true; shift ;;
        --cn|--china) USE_CN_MIRROR=true; BUILD=true; shift ;;
        --no-migrate) SKIP_MIGRATE=true; shift ;;
        --env)
            if [ -z "${2:-}" ] || [[ "${2:-}" =~ ^-- ]]; then
                log_error "--env requires a file path"
                exit 2
            fi
            EXPLICIT_ENV_FILE=true
            ENV_FILE="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--build] [--cn] [--pull] [--infra] [--app] [--no-migrate] [--env FILE]"
            exit 0 ;;
        *) log_error "Unknown option: $1"; exit 1 ;;
    esac
done

if [ "$EXPLICIT_ENV_FILE" = true ]; then
    require_env_file
fi

load_env

if [ "$INFRA_ONLY" = true ] && [ "$APP_ONLY" = true ]; then
    log_error "--infra and --app cannot be used together. Run them as separate commands."
    exit 2
fi

if [ "$SKIP_MIGRATE" != true ] && [ "$INFRA_ONLY" != true ]; then
    SHOULD_MIGRATE=true
fi

# -- Pre-flight checks -------------------------------------------------------
log_step "Pre-flight checks"
require_docker
require_env_file
load_env
if [ "$INFRA_ONLY" = true ]; then
    "$SCRIPT_DIR/validate-env.sh" --env "$ENV_FILE" --infra-only --config-only
else
    "$SCRIPT_DIR/validate-env.sh" --env "$ENV_FILE" --config-only
fi

read -r -a COMPOSE_CMD <<< "$(get_compose_cmd)"
COMPOSE_FILES=(-f docker-compose.yml)
if [ "$BUILD" = true ]; then
    COMPOSE_FILES+=(-f docker-compose.build.yml)
fi

compose() {
    "${COMPOSE_CMD[@]}" "${COMPOSE_FILES[@]}" --env-file "$ENV_FILE" "$@"
}

cd "$PROJECT_ROOT"

INFRA_SERVICES="postgres redis qdrant"
FULL_APP_SERVICES="gateway frontend knowledge-service knowledge-worker assistant-service"

assert_compose_owner

# -- Determine services to deploy --------------------------------------------
SERVICES=""
if [ "$INFRA_ONLY" = true ]; then
    SERVICES="$INFRA_SERVICES"
elif [ "$APP_ONLY" = true ]; then
    SERVICES="gateway frontend knowledge-service knowledge-worker assistant-service"
fi

# -- Pull base images --------------------------------------------------------
if [ "$PULL" = true ] && [ "$BUILD" != true ]; then
    log_step "Pulling configured versioned images"
    # shellcheck disable=SC2086
    compose pull $SERVICES
fi

# -- Build if requested ------------------------------------------------------
if [ "$BUILD" = true ]; then
    log_step "Building Docker images"
    BUILD_ARGS=()
    if [ "$PULL" = true ]; then
        BUILD_ARGS+=(--pull)
    fi
    if [ "$USE_CN_MIRROR" = true ]; then
        log_info "Using China mirrors (PyPI + NPM)"
        export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
        export PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
        export NPM_REGISTRY=https://registry.npmmirror.com
        export DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn
    fi
    # shellcheck disable=SC2086
    compose build "${BUILD_ARGS[@]}" $SERVICES
fi

# The shell migration runner is the schema owner. Stop app services first so a
# previous run cannot race migrations during startup.
if [ "$INFRA_ONLY" != true ]; then
    log_step "Stopping application services before migrations"
    if [ "$APP_ONLY" = true ]; then
        # shellcheck disable=SC2086
        compose stop $SERVICES >/dev/null || true
    else
        # shellcheck disable=SC2086
        compose stop $FULL_APP_SERVICES >/dev/null || true
    fi
fi

# -- Start services ----------------------------------------------------------
if [ "$INFRA_ONLY" = true ]; then
    START_SERVICES="$INFRA_SERVICES"
elif [ "$APP_ONLY" = true ]; then
    START_SERVICES=""
else
    START_SERVICES="$INFRA_SERVICES"
fi

log_step "Starting services"
if [ -n "$START_SERVICES" ]; then
    # shellcheck disable=SC2086
    compose up -d --remove-orphans $START_SERVICES
else
    log_info "Application services will start after migrations"
fi

# -- Wait for infrastructure to be healthy -----------------------------------
log_step "Health checks"

if [ "$APP_ONLY" != true ] || [ "$SHOULD_MIGRATE" = true ]; then
    wait_for_healthy "PostgreSQL" "check_postgres_health" 30
fi

if [ "$APP_ONLY" != true ]; then
    wait_for_healthy "Redis" "check_redis_health" 30
    wait_for_healthy "Qdrant" "check_qdrant_health" 30 || log_warn "Qdrant may still be starting"
fi

# -- Run database migrations -------------------------------------------------
if [ "$SHOULD_MIGRATE" = true ]; then
    log_step "Running database migrations"
    ENV_FILE="$ENV_FILE" "$(dirname "$0")/migrate.sh" --auto
fi

if [ "$INFRA_ONLY" != true ]; then
    log_step "Starting application services"
    if [ "$APP_ONLY" = true ]; then
        # shellcheck disable=SC2086
        compose up -d --remove-orphans $SERVICES
    else
        # shellcheck disable=SC2086
        compose up -d --remove-orphans $FULL_APP_SERVICES
    fi
fi

# -- Wait for application health ---------------------------------------------
if [ "$INFRA_ONLY" != true ]; then
    wait_for_healthy "Knowledge service" "check_knowledge_health" 60 || log_warn "Knowledge service may still be starting"
    wait_for_healthy "Knowledge worker" "check_knowledge_worker_health" 60 || log_warn "Knowledge worker may still be starting"
    wait_for_healthy "Assistant service" "check_assistant_health" 60 || log_warn "Assistant service may still be starting"
    wait_for_healthy "Bundled docgen plugin" "check_docgen_health" 60 || log_warn "Bundled docgen plugin may still be starting"
    wait_for_healthy "Gateway" "check_gateway_health" 60 || log_warn "Gateway may still be starting"
    wait_for_healthy "Frontend" "check_frontend_health" 30 || log_warn "Frontend may still be starting"
fi

if [ "$INFRA_ONLY" = true ]; then
    "$SCRIPT_DIR/validate-env.sh" --env "$ENV_FILE" --infra-only --runtime
else
    "$SCRIPT_DIR/validate-env.sh" --env "$ENV_FILE" --runtime
fi

# -- Summary -----------------------------------------------------------------
log_step "Deployment complete"
compose ps
echo ""
log_success "AI Gateway is running"
echo ""
echo "  Frontend:  http://localhost:${FRONTEND_PORT:-8081}"
echo "  Backend:   http://localhost:${GATEWAY_PORT:-8080}"
echo "  API Docs:  http://localhost:${GATEWAY_PORT:-8080}/docs"
echo ""
