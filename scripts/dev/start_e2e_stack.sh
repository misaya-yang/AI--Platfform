#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
VERIFY_SCRIPT="$ROOT_DIR/scripts/dev/verify_local_stack.py"
DEV_SETUP_SCRIPT="$ROOT_DIR/scripts/new/setup-dev.sh"

find_free_port() {
  python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

find_conda() {
  if [ -x "$HOME/miniconda3/bin/conda" ]; then
    printf '%s\n' "$HOME/miniconda3/bin/conda"
    return 0
  fi
  if which conda >/dev/null 2>&1; then
    which conda
    return 0
  fi
  return 1
}

pick_conda_env() {
  local conda_bin="$1"
  local env_name
  env_name="$($conda_bin env list | awk '$1 == "ai_gateway" { print $1; exit }')"
  if [ -n "$env_name" ]; then
    printf '%s\n' "$env_name"
    return 0
  fi
  if [ -n "${CONDA_DEFAULT_ENV:-}" ]; then
    printf '%s\n' "$CONDA_DEFAULT_ENV"
    return 0
  fi
  return 1
}

infer_backend_target() {
  if rg -n "def create_app\(" "$ROOT_DIR/src/main.py" >/dev/null 2>&1; then
    printf '%s\n' "src.main:create_app --factory"
    return 0
  fi
  printf '%s\n' "src.main:app"
}

ensure_e2e_env() {
  export E2E_BACKEND_PORT="${E2E_BACKEND_PORT:-$(find_free_port)}"
  export E2E_FRONTEND_PORT="${E2E_FRONTEND_PORT:-$(find_free_port)}"
  export E2E_API_URL="${E2E_API_URL:-http://127.0.0.1:${E2E_BACKEND_PORT}}"
  export E2E_BASE_URL="${E2E_BASE_URL:-http://127.0.0.1:${E2E_FRONTEND_PORT}}"
  export VITE_API_URL="$E2E_API_URL"
}

ensure_dev_dependencies() {
  if [ "${E2E_SKIP_DEV_DEPENDENCIES:-0}" = "1" ]; then
    return 0
  fi
  if [ ! -x "$DEV_SETUP_SCRIPT" ]; then
    echo "Missing dev setup script: $DEV_SETUP_SCRIPT" >&2
    exit 1
  fi
  echo "Bootstrapping dev dependencies for E2E..." >&2
  "$DEV_SETUP_SCRIPT" --start >&2
}

run_backend() {
  ensure_e2e_env
  ensure_dev_dependencies
  local conda_bin
  conda_bin="$(find_conda)" || {
    echo "Unable to find conda. Expected ~/miniconda3/bin/conda or conda on PATH." >&2
    exit 1
  }
  local conda_env
  conda_env="$(pick_conda_env "$conda_bin")" || {
    echo "Unable to detect conda env. Activate the project env or create ai_gateway." >&2
    exit 1
  }
  local backend_target
  backend_target="$(infer_backend_target)"
  cd "$ROOT_DIR"
  exec "$conda_bin" run -n "$conda_env" python -m uvicorn $backend_target --host 127.0.0.1 --port "$E2E_BACKEND_PORT"
}

run_frontend() {
  ensure_e2e_env
  cd "$ROOT_DIR"
  exec env -u VITE_API_BASE_URL VITE_API_URL="$E2E_API_URL" pnpm -C "$WEB_DIR" dev --host 127.0.0.1 --port "$E2E_FRONTEND_PORT"
}

run_verify() {
  ensure_e2e_env
  cd "$ROOT_DIR"
  exec python3 "$VERIFY_SCRIPT" --api-url "$E2E_API_URL"
}

run_tests() {
  ensure_e2e_env
  cd "$ROOT_DIR"
  if [ "${1:-}" = "--" ]; then
    shift
  fi
  exec pnpm -C "$WEB_DIR" exec playwright test "$@"
}

main() {
  local command="${1:-test}"
  shift || true
  case "$command" in
    backend)
      run_backend "$@"
      ;;
    frontend)
      run_frontend "$@"
      ;;
    verify)
      run_verify "$@"
      ;;
    test)
      run_tests "$@"
      ;;
    *)
      echo "Usage: $0 [backend|frontend|verify|test] [playwright args...]" >&2
      exit 2
      ;;
  esac
}

main "$@"
