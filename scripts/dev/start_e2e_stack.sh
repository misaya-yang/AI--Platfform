#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_DIR="$ROOT_DIR/web"
VERIFY_SCRIPT="$ROOT_DIR/scripts/dev/verify_local_stack.py"
DEV_SETUP_SCRIPT="$ROOT_DIR/scripts/new/setup-dev.sh"
PNPM_VERSION="${PNPM_VERSION:-10.33.0}"

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
  export E2E_KNOWLEDGE_PORT="${E2E_KNOWLEDGE_PORT:-8092}"
  export E2E_ASSISTANT_PORT="${E2E_ASSISTANT_PORT:-8093}"
  export E2E_MCP_DOCGEN_PORT="${E2E_MCP_DOCGEN_PORT:-8765}"
  export E2E_API_URL="${E2E_API_URL:-http://127.0.0.1:${E2E_BACKEND_PORT}}"
  export E2E_BASE_URL="${E2E_BASE_URL:-http://127.0.0.1:${E2E_FRONTEND_PORT}}"
  export E2E_KNOWLEDGE_URL="${E2E_KNOWLEDGE_URL:-http://127.0.0.1:${E2E_KNOWLEDGE_PORT}}"
  export E2E_ASSISTANT_URL="${E2E_ASSISTANT_URL:-http://127.0.0.1:${E2E_ASSISTANT_PORT}}"
  export E2E_MCP_DOCGEN_URL="${E2E_MCP_DOCGEN_URL:-http://127.0.0.1:${E2E_MCP_DOCGEN_PORT}}"
  export VITE_API_URL="$E2E_API_URL"
  export CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-${E2E_BASE_URL},http://localhost:3000,http://127.0.0.1:3000}"
  export ASSISTANT_SERVICE_URL="${E2E_ASSISTANT_SERVICE_URL:-${E2E_ASSISTANT_URL}}"
  export KB_SERVICE_URL="${E2E_KB_SERVICE_URL:-${E2E_KNOWLEDGE_URL}}"
  export MCP_DOCGEN_SERVICE_URL="${E2E_MCP_DOCGEN_SERVICE_URL:-${E2E_MCP_DOCGEN_URL}}"
}

json_array() {
  python3 - "$@" <<'PY'
import json
import sys

print(json.dumps([item for item in sys.argv[1:] if item]))
PY
}

database_url() {
  local url="${DATABASE_URL:-${GATEWAY_DATABASE__DSN:-}}"
  if [ -z "$url" ]; then
    echo "Missing DATABASE_URL or GATEWAY_DATABASE__DSN for E2E service startup." >&2
    exit 1
  fi
  printf '%s\n' "$url"
}

redis_url() {
  printf '%s\n' "${REDIS_URL:-${GATEWAY_REDIS__URL:-}}"
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
  export PYTHONPATH="$ROOT_DIR/packages/ai-gateway-core/src:${PYTHONPATH:-}"
  export QUIZ_DETERMINISTIC_FALLBACK_ENABLED="${QUIZ_DETERMINISTIC_FALLBACK_ENABLED:-1}"
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

run_knowledge() {
  ensure_e2e_env
  ensure_dev_dependencies
  export PYTHONPATH="$ROOT_DIR/apps/knowledge-service/src:$ROOT_DIR/packages/ai-gateway-core/src:${PYTHONPATH:-}"
  export DATABASE_URL="$(database_url)"
  export KNOWLEDGE_DATABASE__DSN="$DATABASE_URL"
  export KNOWLEDGE_QDRANT__URL="${KNOWLEDGE_QDRANT__URL:-http://127.0.0.1:${QDRANT_HTTP_PORT:-${QDRANT_PORT:-6333}}}"
  export KNOWLEDGE_REDIS__ENABLED="${KNOWLEDGE_REDIS__ENABLED:-true}"
  export KNOWLEDGE_REDIS__URL="${KNOWLEDGE_REDIS__URL:-$(redis_url)}"
  export KNOWLEDGE_CORS__ALLOW_ORIGINS="$(json_array "$E2E_BASE_URL" "http://localhost:3000" "http://127.0.0.1:3000")"
  export KNOWLEDGE_EMBEDDINGS__GOOGLE_API_KEY="${KNOWLEDGE_EMBEDDINGS__GOOGLE_API_KEY:-${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}}"
  export KNOWLEDGE_EMBEDDINGS__DASHSCOPE_API_KEY="${KNOWLEDGE_EMBEDDINGS__DASHSCOPE_API_KEY:-${DASHSCOPE_API_KEY:-}}"
  export KNOWLEDGE_EMBEDDINGS__API_KEY="${KNOWLEDGE_EMBEDDINGS__API_KEY:-${KNOWLEDGE_EMBEDDINGS__GOOGLE_API_KEY:-${KNOWLEDGE_EMBEDDINGS__DASHSCOPE_API_KEY:-}}}"
  export KNOWLEDGE_OCR__ENABLED="${KNOWLEDGE_OCR__ENABLED:-false}"

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
  cd "$ROOT_DIR"
  exec "$conda_bin" run --no-capture-output -n "$conda_env" python -m uvicorn knowledge_service.main:app --host 127.0.0.1 --port "$E2E_KNOWLEDGE_PORT"
}

run_assistant() {
  ensure_e2e_env
  ensure_dev_dependencies
  export PYTHONPATH="$ROOT_DIR/apps/assistant-service/src:$ROOT_DIR/packages/ai-gateway-core/src:${PYTHONPATH:-}"
  export ASSISTANT_E2E_STUB_LLM="${ASSISTANT_E2E_STUB_LLM:-1}"
  export DATABASE_URL="$(database_url)"
  export REDIS_URL="${REDIS_URL:-$(redis_url)}"
  export ASSISTANT_DATABASE__DSN="$DATABASE_URL"
  export ASSISTANT_REDIS__ENABLED="${ASSISTANT_REDIS__ENABLED:-true}"
  export ASSISTANT_REDIS__URL="${ASSISTANT_REDIS__URL:-$REDIS_URL}"
  export ASSISTANT_KB__URL="${ASSISTANT_KB__URL:-$E2E_KNOWLEDGE_URL}"
  export KB_SERVICE_URL="$E2E_KNOWLEDGE_URL"
  export ASSISTANT_CORS__ALLOW_ORIGINS="$(json_array "$E2E_BASE_URL" "http://localhost:3000" "http://127.0.0.1:3000")"
  export ASSISTANT_REQUIRE_DB="${ASSISTANT_REQUIRE_DB:-true}"
  export ASSISTANT_REQUIRE_REDIS="${ASSISTANT_REQUIRE_REDIS:-false}"

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
  cd "$ROOT_DIR"
  exec "$conda_bin" run --no-capture-output -n "$conda_env" python -m uvicorn assistant_service.main:app --host 127.0.0.1 --port "$E2E_ASSISTANT_PORT"
}

run_mcp_docgen() {
  ensure_e2e_env
  export PYTHONPATH="$ROOT_DIR/packages/mcp-docgen-server/src:${PYTHONPATH:-}"
  export MCP_TRANSPORT="${MCP_TRANSPORT:-http}"
  export PORT="$E2E_MCP_DOCGEN_PORT"
  export DOCGEN_PUBLIC_URL="${DOCGEN_PUBLIC_URL:-$E2E_MCP_DOCGEN_URL}"

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
  cd "$ROOT_DIR"
  exec "$conda_bin" run --no-capture-output -n "$conda_env" python -m mcp_docgen_server
}

run_frontend() {
  ensure_e2e_env
  cd "$ROOT_DIR"
  if command -v corepack >/dev/null 2>&1; then
    exec env -u VITE_API_BASE_URL VITE_API_URL="$E2E_API_URL" corepack "pnpm@$PNPM_VERSION" -C "$WEB_DIR" dev --host 127.0.0.1 --port "$E2E_FRONTEND_PORT"
  fi
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
  if command -v corepack >/dev/null 2>&1; then
    exec corepack "pnpm@$PNPM_VERSION" -C "$WEB_DIR" exec playwright test "$@"
  fi
  exec pnpm -C "$WEB_DIR" exec playwright test "$@"
}

main() {
  local command="${1:-test}"
  shift || true
  case "$command" in
    mcp-docgen)
      run_mcp_docgen "$@"
      ;;
    knowledge)
      run_knowledge "$@"
      ;;
    assistant)
      run_assistant "$@"
      ;;
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
      echo "Usage: $0 [mcp-docgen|knowledge|assistant|backend|frontend|verify|test] [playwright args...]" >&2
      exit 2
      ;;
  esac
}

main "$@"
