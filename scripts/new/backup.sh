#!/bin/bash
# =============================================================================
# AI Gateway - Database Backup & Restore
# =============================================================================
# Usage:  make backup          / make restore
#   or:   ./scripts/new/backup.sh [OPTIONS]
#
# Options:
#   (default)           Create a new backup
#   --restore [file]    Restore from backup (latest if no file specified)
#   --list              List available backups
# =============================================================================

source "$(dirname "$0")/common.sh"
load_env

BACKUP_DIR="${PROJECT_ROOT}/backups"
mkdir -p "$BACKUP_DIR"

RESTORE=false
LIST=false
BACKUP_FILE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --restore)
            RESTORE=true; shift
            if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
                BACKUP_FILE="$1"; shift
            fi ;;
        --list) LIST=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--restore [file]] [--list]"
            exit 0 ;;
        *) shift ;;
    esac
done

# -- List backups ------------------------------------------------------------
if [ "$LIST" = true ]; then
    echo "Available backups:"
    ls -lh "$BACKUP_DIR"/*.sql.gz 2>/dev/null || echo "  (none)"
    exit 0
fi

# -- Restore -----------------------------------------------------------------
if [ "$RESTORE" = true ]; then
    if [ -z "$BACKUP_FILE" ]; then
        BACKUP_FILE=$(ls -t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | head -1)
        if [ -z "$BACKUP_FILE" ]; then
            log_error "No backup files found in $BACKUP_DIR"
            exit 1
        fi
    fi

    [ -f "$BACKUP_FILE" ] || { log_error "File not found: $BACKUP_FILE"; exit 1; }

    log_warn "This will overwrite the current database!"
    confirm "Restore from $(basename "$BACKUP_FILE")?" || { log_info "Cancelled"; exit 0; }

    log_info "Restoring from $(basename "$BACKUP_FILE")..."
    gunzip -c "$BACKUP_FILE" | docker exec -i "$(pg_container)" psql -U "$(pg_user)" -d "$(pg_database)"
    log_success "Database restored"
    exit 0
fi

# -- Create backup -----------------------------------------------------------
log_step "Creating database backup"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/gateway_${TIMESTAMP}.sql.gz"

docker exec "$(pg_container)" pg_dump -U "$(pg_user)" -d "$(pg_database)" | gzip > "$BACKUP_FILE"

if [ "${PIPESTATUS[0]}" -ne 0 ]; then
    rm -f "$BACKUP_FILE"
    log_error "pg_dump failed — backup aborted"
    exit 1
fi

BACKUP_SIZE=$(ls -lh "$BACKUP_FILE" | awk '{print $5}')
log_success "Backup created: $(basename "$BACKUP_FILE") ($BACKUP_SIZE)"

# Cleanup: keep last 7
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "*.sql.gz" | wc -l | tr -d ' ')
if [ "$BACKUP_COUNT" -gt 7 ]; then
    log_info "Cleaning up old backups (keeping last 7)..."
    ls -t "$BACKUP_DIR"/*.sql.gz | tail -n +8 | xargs rm -f
fi
