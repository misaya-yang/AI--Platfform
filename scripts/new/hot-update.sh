#!/bin/bash
# =============================================================================
# AI Gateway - Local Deployment Hot Update
# =============================================================================
# Refresh source code inside the running local deployment containers without
# rebuilding images or running pip. Use this for Python/source-only changes.
#
# Usage:
#   make hot-update
#   make hot-update ARGS="--assistant --gateway"
#   make hot-update ARGS="--frontend"
#
# For dependency, Dockerfile, base image, or lockfile changes, use a rebuild.
# For continuous edit/reload, use: make dev-compose
# =============================================================================

source "$(dirname "$0")/common.sh"

UPDATE_GATEWAY=false
UPDATE_ASSISTANT=false
UPDATE_KNOWLEDGE=false
UPDATE_DOCGEN=false
UPDATE_FRONTEND=false
NO_RESTART=false
EXPLICIT_SERVICE=false
EXPLICIT_ENV_FILE=false

usage() {
    cat <<'EOF'
Usage: scripts/new/hot-update.sh [OPTIONS]

Options:
  --gateway       Copy gateway src/config/database and shared core, restart gateway
  --assistant     Copy assistant-service src and shared core, restart assistant
  --knowledge     Copy knowledge-service package and shared core, restart knowledge
  --docgen        Copy bundled docgen/plugin source into Assistant, restart Assistant
  --frontend      Build web/dist locally and copy it into the nginx container
  --python        Update all Python services (gateway, assistant, knowledge, bundled docgen)
  --all           Update all supported services, including frontend
  --no-restart    Copy files only
  --env FILE      Use a specific env file instead of .env
  -h, --help      Show this help

Default with no service flag: --python
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gateway) UPDATE_GATEWAY=true; EXPLICIT_SERVICE=true; shift ;;
        --assistant) UPDATE_ASSISTANT=true; EXPLICIT_SERVICE=true; shift ;;
        --knowledge) UPDATE_KNOWLEDGE=true; EXPLICIT_SERVICE=true; shift ;;
        --docgen) UPDATE_DOCGEN=true; EXPLICIT_SERVICE=true; shift ;;
        --frontend) UPDATE_FRONTEND=true; EXPLICIT_SERVICE=true; shift ;;
        --python)
            UPDATE_GATEWAY=true
            UPDATE_ASSISTANT=true
            UPDATE_KNOWLEDGE=true
            UPDATE_DOCGEN=true
            EXPLICIT_SERVICE=true
            shift
            ;;
        --all)
            UPDATE_GATEWAY=true
            UPDATE_ASSISTANT=true
            UPDATE_KNOWLEDGE=true
            UPDATE_DOCGEN=true
            UPDATE_FRONTEND=true
            EXPLICIT_SERVICE=true
            shift
            ;;
        --no-restart) NO_RESTART=true; shift ;;
        --env)
            if [ -z "${2:-}" ] || [[ "${2:-}" =~ ^-- ]]; then
                log_error "--env requires a file path"
                exit 2
            fi
            EXPLICIT_ENV_FILE=true
            ENV_FILE="$2"
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "Unknown option: $1"; usage; exit 2 ;;
    esac
done

if [ "$EXPLICIT_SERVICE" != true ]; then
    UPDATE_GATEWAY=true
    UPDATE_ASSISTANT=true
    UPDATE_KNOWLEDGE=true
    UPDATE_DOCGEN=true
fi

if [ "$EXPLICIT_ENV_FILE" = true ]; then
    require_env_file
fi

load_env

log_step "Pre-flight checks"
require_docker
require_env_file
assert_compose_owner
COMPOSE_CMD=$(get_compose_cmd)
cd "$PROJECT_ROOT"

container_must_run() {
    local container="$1"
    if ! docker inspect "$container" >/dev/null 2>&1; then
        log_error "Container is missing: $container. Start the local deployment first."
        exit 1
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != "true" ]; then
        log_error "Container is not running: $container"
        exit 1
    fi
}

site_packages() {
    local container="$1"
    docker exec -i "$container" python - <<'PY' | tr -d '\r'
import site
import sys

paths = site.getsitepackages()
if paths:
    print(paths[0])
else:
    for path in sys.path:
        if path.endswith("site-packages"):
            print(path)
            break
PY
}

copy_dir() {
    local source="$1"
    local container="$2"
    local destination="$3"
    local owner="${4:-}"

    if [ ! -d "$source" ]; then
        log_error "Source directory not found: $source"
        exit 1
    fi

    container_must_run "$container"
    docker exec -u root "$container" mkdir -p "$destination"
    docker cp "$source/." "$container:$destination/"
    if [ -n "$owner" ]; then
        docker exec -u root "$container" chown -R "$owner" "$destination" >/dev/null 2>&1 || true
    fi
    log_success "Copied $source -> $container:$destination"
}

warn_dependency_changes() {
    if ! command -v git >/dev/null 2>&1 || ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    local changed
    changed="$(git diff --name-only -- \
        pyproject.toml uv.lock Dockerfile docker-compose.yml docker-compose.dev.yml \
        apps/assistant-service/pyproject.toml apps/assistant-service/Dockerfile \
        apps/knowledge-service/pyproject.toml apps/knowledge-service/Dockerfile \
        packages/mcp-docgen-server/pyproject.toml \
        web/package.json web/pnpm-lock.yaml web/Dockerfile 2>/dev/null || true)"
    if [ -n "$changed" ]; then
        log_warn "Dependency/Docker/build files changed; hot-update will not install dependencies or rebuild images."
        printf '%s\n' "$changed" | sed 's/^/  - /'
    fi
}

restart_services=()

log_step "Copying source into running containers"
warn_dependency_changes

if [ "$UPDATE_GATEWAY" = true ]; then
    gateway="$(gateway_container)"
    gateway_site="$(site_packages "$gateway")"
    copy_dir "src" "$gateway" "/app/src" "appuser:appuser"
    copy_dir "config" "$gateway" "/app/config" "appuser:appuser"
    copy_dir "database" "$gateway" "/app/database" "appuser:appuser"
    copy_dir "packages/ai-gateway-core/src/ai_gateway_core" "$gateway" "$gateway_site/ai_gateway_core"
    restart_services+=("gateway")
fi

if [ "$UPDATE_ASSISTANT" = true ]; then
    assistant="$(assistant_container)"
    assistant_site="$(site_packages "$assistant")"
    copy_dir "apps/assistant-service/src/assistant_service" "$assistant" "/app/apps/assistant-service/src/assistant_service" "appuser:appuser"
    copy_dir "packages/ai-gateway-core/src/ai_gateway_core" "$assistant" "$assistant_site/ai_gateway_core"
    restart_services+=("assistant-service")
fi

if [ "$UPDATE_KNOWLEDGE" = true ]; then
    knowledge="$(knowledge_container)"
    knowledge_site="$(site_packages "$knowledge")"
    copy_dir "apps/knowledge-service/src/knowledge_service" "$knowledge" "$knowledge_site/knowledge_service"
    copy_dir "packages/ai-gateway-core/src/ai_gateway_core" "$knowledge" "$knowledge_site/ai_gateway_core"
    restart_services+=("knowledge-service")
fi

if [ "$UPDATE_DOCGEN" = true ]; then
    assistant="$(assistant_container)"
    assistant_site="$(site_packages "$assistant")"
    copy_dir "packages/mcp-docgen-server/src/docgen" "$assistant" "$assistant_site/docgen" "appuser:appuser"
    copy_dir "packages/mcp-docgen-server/src/mcp_docgen_server" "$assistant" "$assistant_site/mcp_docgen_server" "appuser:appuser"
    copy_dir "agent-plugins/ai-docgen" "$assistant" "/opt/agent-plugins/ai-docgen"
    copy_dir "agent-plugins/community-doublecheck" "$assistant" "/opt/agent-plugins/community-doublecheck"
    copy_dir "agent-plugins/community-engineering-reviewers" "$assistant" "/opt/agent-plugins/community-engineering-reviewers"
    if [ "$UPDATE_ASSISTANT" != true ]; then
        restart_services+=("assistant-service")
    fi
fi

if [ "$UPDATE_FRONTEND" = true ]; then
    frontend="$(frontend_container)"
    container_must_run "$frontend"
    log_step "Building frontend assets locally"
    corepack pnpm@10.33.0 -C web build
    copy_dir "web/dist" "$frontend" "/usr/share/nginx/html"
    docker exec "$frontend" nginx -s reload >/dev/null 2>&1 || restart_services+=("frontend")
fi

if [ "$NO_RESTART" = true ]; then
    log_warn "Skipped service restart because --no-restart was set."
else
    if [ "${#restart_services[@]}" -gt 0 ]; then
        log_step "Restarting updated services"
        # shellcheck disable=SC2086
        $COMPOSE_CMD --env-file "$ENV_FILE" restart "${restart_services[@]}"
    fi
fi

log_step "Runtime health checks"
if [ "$UPDATE_KNOWLEDGE" = true ]; then
    wait_for_healthy "Knowledge service" "check_knowledge_health" 60 || log_warn "Knowledge service may still be starting"
fi
if [ "$UPDATE_ASSISTANT" = true ]; then
    wait_for_healthy "Assistant service" "check_assistant_health" 60 || log_warn "Assistant service may still be starting"
fi
if [ "$UPDATE_DOCGEN" = true ] && [ "$UPDATE_ASSISTANT" != true ]; then
    wait_for_healthy "Assistant service with bundled docgen" "check_assistant_health" 60 || log_warn "Assistant service may still be starting"
fi
if [ "$UPDATE_GATEWAY" = true ]; then
    wait_for_healthy "Gateway" "check_gateway_health" 60 || log_warn "Gateway may still be starting"
fi
if [ "$UPDATE_FRONTEND" = true ]; then
    wait_for_healthy "Frontend" "check_frontend_health" 30 || log_warn "Frontend may still be starting"
fi

log_success "Hot update complete. No pip install or image rebuild was run."
