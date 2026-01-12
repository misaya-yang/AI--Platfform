#!/bin/bash
# =============================================================================
# AI Gateway - Backup Script
# =============================================================================
# Usage:
#   ./scripts/backup.sh              # Create backup
#   ./scripts/backup.sh --restore    # Restore from latest backup
#   ./scripts/backup.sh --list       # List available backups
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Backup directory
BACKUP_DIR="${PROJECT_ROOT}/backups"
mkdir -p "$BACKUP_DIR"

# Load environment
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Default values
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-111111}
POSTGRES_DB=${POSTGRES_DB:-gateway}

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Parse arguments
RESTORE=false
LIST=false
BACKUP_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --restore)
            RESTORE=true
            shift
            if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
                BACKUP_FILE="$1"
                shift
            fi
            ;;
        --list)
            LIST=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --restore [file]  Restore from backup (latest if no file specified)"
            echo "  --list            List available backups"
            echo "  -h, --help        Show this help"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# List backups
if [ "$LIST" = true ]; then
    echo "=== Available Backups ==="
    ls -lh "$BACKUP_DIR"/*.sql.gz 2>/dev/null || echo "No backups found"
    exit 0
fi

# Restore backup
if [ "$RESTORE" = true ]; then
    if [ -z "$BACKUP_FILE" ]; then
        BACKUP_FILE=$(ls -t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | head -1)
        if [ -z "$BACKUP_FILE" ]; then
            log_error "No backup files found in $BACKUP_DIR"
            exit 1
        fi
    fi

    if [ ! -f "$BACKUP_FILE" ]; then
        log_error "Backup file not found: $BACKUP_FILE"
        exit 1
    fi

    log_warn "This will overwrite the current database!"
    read -p "Are you sure you want to restore from $(basename $BACKUP_FILE)? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Restore cancelled"
        exit 0
    fi

    log_info "Restoring from $BACKUP_FILE..."

    gunzip -c "$BACKUP_FILE" | docker exec -i ai-gateway-pg psql -U $POSTGRES_USER -d $POSTGRES_DB

    log_success "Database restored successfully"
    exit 0
fi

# Create backup
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/gateway_${TIMESTAMP}.sql.gz"

log_info "Creating database backup..."

docker exec ai-gateway-pg pg_dump -U $POSTGRES_USER -d $POSTGRES_DB | gzip > "$BACKUP_FILE"

BACKUP_SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
log_success "Backup created: $(basename $BACKUP_FILE) ($BACKUP_SIZE)"

# Clean up old backups (keep last 7)
BACKUP_COUNT=$(ls -1 "$BACKUP_DIR"/*.sql.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt 7 ]; then
    log_info "Cleaning up old backups (keeping last 7)..."
    ls -t "$BACKUP_DIR"/*.sql.gz | tail -n +8 | xargs rm -f
fi

log_success "Backup completed"
