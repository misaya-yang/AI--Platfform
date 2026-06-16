#!/bin/bash
# =============================================================================
# AI Gateway - Service Status & Health Check
# =============================================================================
# Usage:  make status
#   or:   ./scripts/new/status.sh
# =============================================================================

source "$(dirname "$0")/common.sh"
load_env

COMPOSE_CMD=$(get_compose_cmd)
cd "$PROJECT_ROOT"

echo ""
echo "=== Docker Compose Services ==="
$COMPOSE_CMD ps 2>/dev/null || echo "  (compose services not running)"

echo ""
echo "=== Health Checks ==="

check_and_report() {
    local name="$1"
    local check_fn="$2"
    printf "  %-18s " "$name:"
    if $check_fn 2>/dev/null; then
        echo -e "${GREEN}Healthy${NC}"
    else
        echo -e "${RED}Not available${NC}"
    fi
}

check_and_report "PostgreSQL" check_postgres_health
check_and_report "Redis" check_redis_health
check_and_report "Qdrant" check_qdrant_health
check_and_report "Knowledge" check_knowledge_health
check_and_report "Assistant" check_assistant_health
check_and_report "Docgen" check_docgen_health
check_and_report "Gateway" check_gateway_health
check_and_report "Frontend" check_frontend_health

echo ""
