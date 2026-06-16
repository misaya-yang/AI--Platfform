#!/bin/bash
# =============================================================================
# AI Gateway - Environment and Runtime Validator
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

ENV_FILE="${PROJECT_ROOT}/.env"
RUNTIME_CHECK=false
CONFIG_CHECK=true
INFRA_ONLY=false
ERRORS=0
WARNINGS=0

usage() {
    cat <<'USAGE'
Usage: scripts/new/validate-env.sh [--env FILE] [--config-only] [--runtime] [--infra-only]

Checks the local .env file without printing secret values.

Modes:
  --config-only   Validate required variables and value shape only (default)
  --runtime       Validate config, then verify Docker runtime dependencies
  --infra-only    Validate only PostgreSQL, Redis, and Qdrant requirements
  --env FILE      Use a specific env file instead of .env
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --env)
            ENV_FILE="${2:-}"
            shift 2
            ;;
        --config-only)
            CONFIG_CHECK=true
            RUNTIME_CHECK=false
            shift
            ;;
        --runtime)
            CONFIG_CHECK=true
            RUNTIME_CHECK=true
            shift
            ;;
        --infra-only)
            INFRA_ONLY=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            usage
            exit 2
            ;;
    esac
done

fail() {
    log_error "$1"
    ERRORS=$((ERRORS + 1))
}

warn() {
    log_warn "$1"
    WARNINGS=$((WARNINGS + 1))
}

strip_optional_quotes() {
    local value="$1"
    value="${value%$'\r'}"
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
        value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
}

load_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        fail "Missing env file: $ENV_FILE"
        return
    fi

    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" != *"="* ]] && continue

        local key="${line%%=*}"
        local value="${line#*=}"
        key=$(echo "$key" | xargs)

        if [[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
            value="$(strip_optional_quotes "$value")"
            export "$key=$value"
        fi
    done < "$ENV_FILE"
}

env_value() {
    local key="$1"
    printf '%s' "${!key:-}"
}

is_placeholder() {
    local raw="$1"
    local value
    value="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"

    case "$value" in
        ""|change_me*|changeme*|change-me*|replace_me*|replace-me*|your_*|your-*|*_here|example|example_*|demo|test|password|secret|123456|111111)
            return 0
            ;;
    esac

    return 1
}

require_secret() {
    local key="$1"
    local min_length="$2"
    local value
    value="$(env_value "$key")"

    if is_placeholder "$value"; then
        fail "$key must be set to a non-placeholder secret."
        return
    fi

    if [ "${#value}" -lt "$min_length" ]; then
        fail "$key must be at least ${min_length} characters."
    fi
}

require_non_empty() {
    local key="$1"
    local value
    value="$(env_value "$key")"

    if is_placeholder "$value"; then
        fail "$key must be set."
    fi
}

require_positive_int() {
    local key="$1"
    local default_value="${2:-}"
    local value
    value="$(env_value "$key")"
    value="${value:-$default_value}"

    if ! [[ "$value" =~ ^[0-9]+$ ]] || [ "$value" -le 0 ]; then
        fail "$key must be a positive integer."
    fi
}

require_url() {
    local key="$1"
    local value
    value="$(env_value "$key")"
    [ -z "$value" ] && return

    if ! [[ "$value" =~ ^https?:// ]]; then
        fail "$key must start with http:// or https://."
    fi
}

require_domain() {
    local key="$1"
    local value
    value="$(env_value "$key")"

    if is_placeholder "$value"; then
        fail "$key must be set."
        return
    fi

    if ! [[ "$value" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; then
        fail "$key must be a valid domain name."
    fi
}

require_json_array_of_strings() {
    local key="$1"
    local value
    value="$(env_value "$key")"
    [ -z "$value" ] && return

    if ! [[ "$value" =~ ^\[[[:space:]]*\"[^\"]*\"([[:space:]]*,[[:space:]]*\"[^\"]*\")*[[:space:]]*\]$ ]]; then
        fail "$key must be a JSON array of strings."
    fi
}

has_any_key() {
    local key
    for key in "$@"; do
        if ! is_placeholder "$(env_value "$key")"; then
            return 0
        fi
    done
    return 1
}

validate_ports() {
    local keys=(
        POSTGRES_PORT
        REDIS_PORT
        QDRANT_HTTP_PORT
        QDRANT_GRPC_PORT
        GATEWAY_PORT
        FRONTEND_PORT
        KNOWLEDGE_SERVICE_PORT
    )
    local defaults=(
        5432
        6379
        6333
        6334
        8080
        8081
        8092
    )
    local seen=""
    local index=0
    local key value

    for key in "${keys[@]}"; do
        value="$(env_value "$key")"
        value="${value:-${defaults[$index]}}"
        index=$((index + 1))

        if ! [[ "$value" =~ ^[0-9]+$ ]] || [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
            fail "$key must be a TCP port between 1 and 65535."
            continue
        fi

        if [[ " $seen " == *" $value "* ]]; then
            fail "$key uses duplicate host port $value."
        fi
        seen="$seen $value"
    done
}

validate_compose_config() {
    local compose_cmd
    compose_cmd="$(get_compose_cmd)"
    if ! (cd "$PROJECT_ROOT" && $compose_cmd --env-file "$ENV_FILE" config --quiet); then
        fail "docker compose config validation failed."
    fi
}

validate_config() {
    log_step "Validating configuration"
    load_env_file

    if [ "$ERRORS" -gt 0 ]; then
        return
    fi

    require_secret POSTGRES_PASSWORD 8
    require_secret REDIS_PASSWORD 8
    require_secret JWT_SECRET 32
    require_secret GATEWAY_ASSISTANT_SHARED_SECRET 32
    require_secret DEFAULT_USER_PASSWORD 12
    require_domain AUTH_ALLOWED_EMAIL_DOMAIN

    require_positive_int POSTGRES_PORT 5432
    require_positive_int REDIS_PORT 6379
    require_positive_int QDRANT_HTTP_PORT 6333
    require_positive_int QDRANT_GRPC_PORT 6334
    require_positive_int GATEWAY_PORT 8080
    require_positive_int FRONTEND_PORT 8081
    require_positive_int KNOWLEDGE_SERVICE_PORT 8092

    validate_ports

    if [ "$INFRA_ONLY" = true ]; then
        validate_compose_config
        if [ "$ERRORS" -eq 0 ]; then
            log_success "Infrastructure configuration validation passed"
        fi
        return
    fi

    local embedding_provider="${KB_EMBEDDING_PROVIDER:-gemini}"
    case "$embedding_provider" in
        gemini|dashscope|siliconflow)
            ;;
        *)
            fail "KB_EMBEDDING_PROVIDER must be one of: gemini, dashscope, siliconflow."
            ;;
    esac

    require_non_empty KB_EMBEDDING_API_KEY
    require_positive_int KB_EMBEDDING_DIMENSION 1024

    if ! has_any_key \
        DASHSCOPE_CHAT_API_KEY \
        DASHSCOPE_API_KEY \
        GOOGLE_API_KEY \
        GEMINI_API_KEY \
        OPENAI_API_KEY \
        ANTHROPIC_API_KEY \
        DEEPSEEK_API_KEY; then
        fail "Set at least one chat model API key: DASHSCOPE_CHAT_API_KEY, DASHSCOPE_API_KEY, GOOGLE_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, or DEEPSEEK_API_KEY."
    fi

    require_url ASSISTANT_SERVICE_URL
    require_url KB_SERVICE_URL
    require_url DOCGEN_PUBLIC_URL
    require_url MCP_DOCGEN_SERVICE_URL
    require_json_array_of_strings KNOWLEDGE_CORS_ALLOW_ORIGINS_JSON
    require_json_array_of_strings ASSISTANT_CORS_ALLOW_ORIGINS_JSON

    validate_compose_config

    if [ "$ERRORS" -eq 0 ]; then
        log_success "Configuration validation passed"
    fi
}

validate_runtime() {
    log_step "Validating runtime dependencies"
    require_docker

    wait_for_healthy "PostgreSQL authenticated query" "check_postgres_health" 60 || fail "PostgreSQL runtime check failed."
    wait_for_healthy "Redis authenticated ping" "check_redis_health" 60 || fail "Redis runtime check failed."
    wait_for_healthy "Qdrant health endpoint" "check_qdrant_health" 60 || fail "Qdrant runtime check failed."

    if [ "$INFRA_ONLY" = true ]; then
        if [ "$ERRORS" -eq 0 ]; then
            log_success "Infrastructure runtime validation passed"
        fi
        return
    fi

    wait_for_healthy "Knowledge service" "check_knowledge_health" 60 || fail "Knowledge service runtime check failed."
    wait_for_healthy "Assistant service" "check_assistant_health" 60 || fail "Assistant service runtime check failed."
    wait_for_healthy "MCP docgen service" "check_docgen_health" 60 || fail "MCP docgen runtime check failed."
    wait_for_healthy "Gateway readiness" "check_gateway_health" 60 || fail "Gateway runtime check failed."
    wait_for_healthy "Frontend health endpoint" "check_frontend_health" 60 || fail "Frontend runtime check failed."

    if [ "$ERRORS" -eq 0 ]; then
        log_success "Runtime validation passed"
    fi
}

main() {
    if [ "$CONFIG_CHECK" = true ]; then
        validate_config
    fi

    if [ "$RUNTIME_CHECK" = true ] && [ "$ERRORS" -eq 0 ]; then
        validate_runtime
    fi

    if [ "$ERRORS" -gt 0 ]; then
        log_error "Validation failed with $ERRORS error(s)."
        exit 1
    fi

    if [ "$WARNINGS" -gt 0 ]; then
        log_warn "Validation completed with $WARNINGS warning(s)."
    fi
}

main "$@"
