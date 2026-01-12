#!/bin/bash
# =============================================================================
# AI Gateway - Database Migration Script
# =============================================================================
# Usage:
#   ./scripts/migrate.sh              # Run migrations
#   ./scripts/migrate.sh --init       # Initialize database (first time)
#   ./scripts/migrate.sh --status     # Check migration status
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Load environment
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Default values
POSTGRES_HOST=${POSTGRES_HOST:-localhost}
POSTGRES_PORT=${POSTGRES_PORT:-5432}
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-111111}
POSTGRES_DB=${POSTGRES_DB:-gateway}

DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

# Parse arguments
INIT=false
STATUS=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --init)
            INIT=true
            shift
            ;;
        --status)
            STATUS=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --init     Initialize database schema (first time setup)"
            echo "  --status   Check database connection and tables"
            echo "  -h, --help Show this help"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Check PostgreSQL is running
check_postgres() {
    log_info "Checking PostgreSQL connection..."
    if command -v psql &> /dev/null; then
        PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1" > /dev/null 2>&1
        if [ $? -eq 0 ]; then
            log_success "PostgreSQL connection successful"
            return 0
        fi
    fi

    # Try using docker
    docker exec ai-gateway-pg psql -U $POSTGRES_USER -d $POSTGRES_DB -c "SELECT 1" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        log_success "PostgreSQL connection successful (via Docker)"
        return 0
    fi

    log_error "Cannot connect to PostgreSQL"
    return 1
}

# Show database status
if [ "$STATUS" = true ]; then
    check_postgres
    echo ""
    echo "=== Database Tables ==="
    docker exec ai-gateway-pg psql -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt" 2>/dev/null || \
        PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB -c "\dt"
    exit 0
fi

# Initialize database
if [ "$INIT" = true ]; then
    log_info "Initializing database schema..."

    check_postgres || exit 1

    # Run schema.sql
    if [ -f "database/schema.sql" ]; then
        log_info "Running database/schema.sql..."
        docker exec -i ai-gateway-pg psql -U $POSTGRES_USER -d $POSTGRES_DB < database/schema.sql 2>/dev/null || \
            PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB < database/schema.sql
        log_success "Schema initialized"
    else
        log_error "database/schema.sql not found"
        exit 1
    fi

    exit 0
fi

# Run migrations
log_info "Running database migrations..."

check_postgres || exit 1

# Run migration scripts in order
if [ -d "database/migrations" ]; then
    for migration in database/migrations/*.sql; do
        if [ -f "$migration" ]; then
            log_info "Running migration: $(basename $migration)"
            docker exec -i ai-gateway-pg psql -U $POSTGRES_USER -d $POSTGRES_DB < "$migration" 2>/dev/null || \
                PGPASSWORD=$POSTGRES_PASSWORD psql -h $POSTGRES_HOST -p $POSTGRES_PORT -U $POSTGRES_USER -d $POSTGRES_DB < "$migration"
        fi
    done
    log_success "All migrations completed"
else
    log_info "No migrations directory found"
fi
