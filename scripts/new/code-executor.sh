#!/bin/bash
# Enable, test, or disable the trusted local Assistant code sandbox.

source "$(dirname "$0")/common.sh"

ACTION="${1:-status}"
OVERLAY_FILE="$PROJECT_ROOT/docker-compose.code-executor.yml"

usage() {
    cat <<'EOF'
Usage: scripts/new/code-executor.sh [enable|test|status|disable]

This is a trusted local-development feature. The default uses the configured
Docker daemon (preferring runsc when available). Docker Sandboxes (sbx) is an
explicit opt-in via ASSISTANT_CODE_EXECUTOR_BACKEND=sbx because first launch
may invoke a downloaded host shim and trigger an operating-system prompt.
EOF
}

case "$ACTION" in
    enable|test|status|disable) ;;
    -h|--help) usage; exit 0 ;;
    *) log_error "Unknown action: $ACTION"; usage; exit 2 ;;
esac

load_env
require_docker
require_env_file
assert_compose_owner
COMPOSE_CMD=$(get_compose_cmd)
cd "$PROJECT_ROOT"
export COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"

if [ ! -f "$OVERLAY_FILE" ]; then
    log_error "Missing code-executor overlay: $OVERLAY_FILE"
    exit 1
fi

preserve_running_assistant_image() {
    if [ -n "${ASSISTANT_IMAGE:-}" ]; then
        return
    fi
    local running_image
    running_image=$(docker inspect -f '{{.Config.Image}}' "$(assistant_container)" 2>/dev/null || true)
    if [ -n "$running_image" ]; then
        export ASSISTANT_IMAGE="$running_image"
    fi
}

prepare_runtime() {
    preserve_running_assistant_image

    local backend sandbox_image sbx_socket socket_probe_image workspace
    workspace="$PROJECT_ROOT/tmp/code-sandbox"
    mkdir -p "$workspace"
    # The Assistant runs as uid 1000, which may differ from the host developer
    # uid. Only this ignored scratch root is shared; each execution gets a
    # private mkdtemp child that is deleted after collection.
    chmod 0777 "$workspace"
    export ASSISTANT_SANDBOX_WORKSPACE_HOST
    ASSISTANT_SANDBOX_WORKSPACE_HOST=$(python3 -c \
        'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' \
        "$workspace")

    backend="${ASSISTANT_CODE_EXECUTOR_BACKEND:-auto}"
    if [ "$backend" = "auto" ]; then
        backend="docker"
    fi

    case "$backend" in
        sbx)
            if ! command -v sbx >/dev/null 2>&1; then
                log_error "Docker Sandboxes is not installed; install docker/tap/sbx or select backend=docker"
                exit 1
            fi
            if ! sbx daemon status >/dev/null 2>&1; then
                log_step "Starting Docker Sandboxes daemon with deny-all network policy"
                sbx daemon start --detach --policy deny-all
            fi
            sbx_socket="${SBX_DOCKER_SOCKET:-$HOME/.sbx/run/d/docker.sock}"
            if [ ! -S "$sbx_socket" ]; then
                log_error "Docker Sandboxes API socket is unavailable: $sbx_socket"
                exit 1
            fi
            if [ "$(DOCKER_HOST="unix://$sbx_socket" docker info --format '{{.DefaultRuntime}}')" != "nerdbox" ]; then
                log_error "Docker Sandboxes endpoint is not backed by nerdbox"
                exit 1
            fi
            export ASSISTANT_CODE_EXECUTOR_BACKEND=sbx
            export ASSISTANT_CODE_EXECUTOR_SOCKET="$sbx_socket"
            export SANDBOX_RUNTIME=""
            sandbox_image="${ASSISTANT_CODE_EXECUTOR_IMAGE:-ghcr.io/misaya-yang/ai-gateway-docgen-sandbox:2.0.0}"
            export ASSISTANT_CODE_EXECUTOR_IMAGE="$sandbox_image"
            if ! DOCKER_HOST="unix://$sbx_socket" docker image inspect "$sandbox_image" >/dev/null 2>&1; then
                log_step "Pulling code executor image into Docker Sandboxes"
                if ! DOCKER_HOST="unix://$sbx_socket" docker pull "$sandbox_image"; then
                    if ! docker image inspect "$sandbox_image" >/dev/null 2>&1; then
                        log_error "Code executor image is unavailable from the registry and local Docker"
                        exit 1
                    fi
                    log_warn "Registry pull denied; importing the verified local image into Docker Sandboxes"
                    docker save "$sandbox_image" \
                        | DOCKER_HOST="unix://$sbx_socket" docker load
                fi
            fi
            log_info "Using Docker Sandboxes nerdbox microVMs for code execution"
            ;;
        docker)
            if [ ! -S /var/run/docker.sock ]; then
                log_error "Docker socket is unavailable at /var/run/docker.sock"
                exit 1
            fi
            export ASSISTANT_CODE_EXECUTOR_BACKEND=docker
            export ASSISTANT_CODE_EXECUTOR_SOCKET=/var/run/docker.sock
            if docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'; then
                export SANDBOX_RUNTIME=runsc
                log_info "Using gVisor runsc for code execution"
            else
                export SANDBOX_RUNTIME=""
                log_warn "runsc is unavailable; using hardened runc for trusted local development"
            fi
            ;;
        *)
            log_error "Unsupported code executor backend: $backend"
            exit 2
            ;;
    esac

    export DOCKER_SOCKET_GID
    socket_probe_image="${ASSISTANT_IMAGE:?Assistant image is required for socket probing}"
    # Docker Desktop exposes a host symlink whose GID can differ from the
    # Linux proxy socket mounted into containers. Probe the mounted view so
    # group_add grants exactly the group that owns the socket at runtime.
    DOCKER_SOCKET_GID=$(docker run --rm \
        --read-only \
        --network none \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --volume "$ASSISTANT_CODE_EXECUTOR_SOCKET:/var/run/docker.sock:ro" \
        --entrypoint /opt/venv/bin/python \
        "$socket_probe_image" \
        -c 'import os; print(os.stat("/var/run/docker.sock").st_gid)')

}

wait_for_assistant() {
    local attempt health
    for attempt in $(seq 1 30); do
        health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$(assistant_container)" 2>/dev/null || true)
        if [ "$health" = "healthy" ]; then
            log_success "Assistant is healthy"
            return
        fi
        sleep 2
    done
    log_error "Assistant did not become healthy"
    exit 1
}

show_status() {
    local backend container enabled socket_mounted health
    container="$(assistant_container)"
    if ! docker inspect "$container" >/dev/null 2>&1; then
        log_error "Assistant container is missing"
        exit 1
    fi
    enabled=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
        | grep '^ASSISTANT_CODE_EXECUTOR_ENABLED=' | cut -d= -f2- || true)
    backend=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
        | grep '^ASSISTANT_CODE_EXECUTOR_BACKEND=' | cut -d= -f2- || true)
    socket_mounted=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}yes{{end}}{{end}}' "$container")
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
    log_info "enabled=${enabled:-false} backend=${backend:-none} docker_socket=${socket_mounted:-no} health=$health"
}

run_smoke() {
    docker exec -i "$(assistant_container)" /opt/venv/bin/python - <<'PY'
import asyncio

from assistant_service.core.code_executor import CodeExecutorService


async def main() -> None:
    executor = CodeExecutorService()
    if not executor.is_docker_available():
        raise SystemExit("code executor is unavailable")
    result = await executor.execute(
        code=(
            "from pathlib import Path\n"
            "marker = 'CODE-EXECUTOR-OK'\n"
            "print(marker)\n"
            "Path('/workspace/output/capability-result.txt').write_text(marker)\n"
        )
    )
    files = {item.filename: item.to_text() for item in result.output_files}
    if not result.is_success() or result.stdout.strip() != "CODE-EXECUTOR-OK":
        raise SystemExit(f"code execution failed: status={result.status.value}")
    if files.get("capability-result.txt") != "CODE-EXECUTOR-OK":
        raise SystemExit("code executor artifact mismatch")
    print(
        {
            "status": result.status.value,
            "exit_code": result.exit_code,
            "stdout": result.stdout.strip(),
            "artifacts": sorted(files),
        }
    )


asyncio.run(main())
PY
}

case "$ACTION" in
    enable)
        log_step "Enabling trusted local code executor"
        prepare_runtime
        $COMPOSE_CMD -f docker-compose.yml -f "$OVERLAY_FILE" --env-file "$(env_file_path)" \
            up -d --no-deps --force-recreate assistant-service
        wait_for_assistant
        show_status
        ;;
    test)
        log_step "Testing trusted local code executor"
        show_status
        run_smoke
        log_success "Code executor smoke passed"
        ;;
    status)
        show_status
        ;;
    disable)
        log_step "Disabling trusted local code executor"
        preserve_running_assistant_image
        export ASSISTANT_CODE_EXECUTOR_ENABLED=false
        $COMPOSE_CMD -f docker-compose.yml --env-file "$(env_file_path)" \
            up -d --no-deps --force-recreate assistant-service
        wait_for_assistant
        show_status
        ;;
esac
