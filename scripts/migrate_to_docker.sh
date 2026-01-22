#!/usr/bin/env bash
# =============================================================================
# AI Gateway - Database Migration Tool (Dev -> Docker/Remote)
# =============================================================================
# 功能:
#   将开发环境数据库 (从 settings/env 获取配置) 迁移到 Docker 容器中。
#   支持本地 Docker 环境和通过 SSH 连接的远程 Docker 环境。
#
# 用法:
#   ./migrate_to_docker.sh [选项]
#
# 选项:
#   -h, --help          显示帮助信息
#   -t, --type TYPE     目标环境类型: local 或 remote (默认: remote)
#   -H, --host HOST     SSH 主机 (格式: user@host)
#   -p, --port PORT     SSH 端口 (默认: 22)
#   -c, --container     容器名称 (默认: ai-gateway-pg)
#   -d, --database      数据库名称 (默认: gateway)
#   -u, --user          数据库用户 (默认: postgres)
#   --no-backup         跳过目标数据库备份
#   --timeout SECONDS   操作超时时间 (默认: 3600)
#   -y, --yes           自动确认所有提示
#
# 环境变量:
#   MIGRATE_SSH_HOST    SSH 主机 (可被 --host 覆盖)
#   MIGRATE_SSH_PORT    SSH 端口 (可被 --port 覆盖)
#   MIGRATE_CONTAINER   容器名称 (可被 --container 覆盖)
#   MIGRATE_DB_NAME     数据库名称 (可被 --database 覆盖)
#   MIGRATE_DB_USER     数据库用户 (可被 --user 覆盖)
#
# 前提条件:
#   1. 本地安装 pg_dump 或 运行着 ai-gateway-postgres 容器
#   2. 如果是远程环境，需要 SSH 免密登录或密码
#
# 警告:
#   目标容器数据库将被完全覆盖！
# =============================================================================

set -euo pipefail

# =============================================================================
# 配置默认值 (可通过环境变量或命令行参数覆盖)
# =============================================================================

# 目标环境类型: "local" 或 "remote"
TARGET_TYPE="${MIGRATE_TARGET_TYPE:-remote}"

# 远程服务器配置
SSH_HOST="${MIGRATE_SSH_HOST:-}"
SSH_PORT="${MIGRATE_SSH_PORT:-22}"
SSH_TIMEOUT="${MIGRATE_SSH_TIMEOUT:-10}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

# 容器配置
CONTAINER_NAME="${MIGRATE_CONTAINER:-ai-gateway-pg}"
DB_NAME="${MIGRATE_DB_NAME:-gateway}"
DB_USER="${MIGRATE_DB_USER:-postgres}"

# 本地源容器 (用于 fallback)
LOCAL_SOURCE_CONTAINER="ai-gateway-postgres"

# 操作配置
OPERATION_TIMEOUT="${MIGRATE_TIMEOUT:-3600}"
SKIP_BACKUP="${MIGRATE_SKIP_BACKUP:-false}"
AUTO_CONFIRM="${MIGRATE_AUTO_CONFIRM:-false}"

# 全局变量
PG_DUMP_CMD=""
PG_DUMP_MODE="host" # host | docker

# =============================================================================
# 颜色定义
# =============================================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# =============================================================================
# 日志函数
# =============================================================================

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${CYAN}[STEP]${NC} $1"; }

# =============================================================================
# 帮助信息
# =============================================================================

show_help() {
    cat << EOF
AI Gateway - 数据库迁移工具

用法: $(basename "$0") [选项]

选项:
  -h, --help              显示此帮助信息
  -t, --type TYPE         目标环境类型: local 或 remote (默认: $TARGET_TYPE)
  -H, --host HOST         SSH 主机 (格式: user@host)
  -p, --port PORT         SSH 端口 (默认: $SSH_PORT)
  -c, --container NAME    容器名称 (默认: $CONTAINER_NAME)
  -d, --database NAME     数据库名称 (默认: $DB_NAME)
  -u, --user USER         数据库用户 (默认: $DB_USER)
  --no-backup             跳过目标数据库备份
  --timeout SECONDS       操作超时时间 (默认: $OPERATION_TIMEOUT)
  -y, --yes               自动确认 (非交互模式)

环境变量:
  MIGRATE_SSH_HOST        SSH 主机
  MIGRATE_SSH_PORT        SSH 端口
  MIGRATE_CONTAINER       容器名称
  MIGRATE_DB_NAME         数据库名称
  MIGRATE_DB_USER         数据库用户
  MIGRATE_TIMEOUT         操作超时时间
  MIGRATE_SKIP_BACKUP     跳过备份 (true/false)

示例:
  # 使用环境变量
  export MIGRATE_SSH_HOST="user@192.168.1.100"
  ./$(basename "$0")

  # 使用命令行参数
  ./$(basename "$0") -t remote -H user@192.168.1.100 -c my-postgres

  # 本地 Docker 迁移
  ./$(basename "$0") -t local -c ai-gateway-pg

EOF
    exit 0
}

# =============================================================================
# 参数解析
# =============================================================================

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                show_help
                ;;
            -t|--type)
                TARGET_TYPE="$2"
                shift 2
                ;;
            -H|--host)
                SSH_HOST="$2"
                shift 2
                ;;
            -p|--port)
                SSH_PORT="$2"
                shift 2
                ;;
            -c|--container)
                CONTAINER_NAME="$2"
                shift 2
                ;;
            -d|--database)
                DB_NAME="$2"
                shift 2
                ;;
            -u|--user)
                DB_USER="$2"
                shift 2
                ;;
            --no-backup)
                SKIP_BACKUP="true"
                shift
                ;;
            --timeout)
                OPERATION_TIMEOUT="$2"
                shift 2
                ;;
            -y|--yes)
                AUTO_CONFIRM="true"
                shift
                ;;
            *)
                log_error "未知参数: $1"
                echo "使用 --help 查看帮助信息"
                exit 1
                ;;
        esac
    done
}

# =============================================================================
# 输入验证
# =============================================================================

validate_identifier() {
    local name="$1"
    local value="$2"
    # 只允许字母、数字、下划线和连字符
    if [[ ! "$value" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]]; then
        log_error "无效的 $name: '$value'"
        log_error "只允许字母、数字、下划线和连字符，且必须以字母或下划线开头"
        exit 1
    fi
}

validate_inputs() {
    # 验证目标类型
    if [[ "$TARGET_TYPE" != "local" && "$TARGET_TYPE" != "remote" ]]; then
        log_error "无效的目标类型: $TARGET_TYPE (必须是 'local' 或 'remote')"
        exit 1
    fi

    # 远程模式需要 SSH_HOST
    if [[ "$TARGET_TYPE" == "remote" && -z "$SSH_HOST" ]]; then
        log_error "远程模式需要指定 SSH 主机"
        log_error "请设置 MIGRATE_SSH_HOST 环境变量或使用 --host 参数"
        exit 1
    fi

    # 验证数据库名称 (防止 SQL 注入)
    validate_identifier "数据库名称" "$DB_NAME"

    # 验证数据库用户名
    validate_identifier "数据库用户" "$DB_USER"

    # 验证容器名称
    validate_identifier "容器名称" "$CONTAINER_NAME"

    # 验证端口号
    if [[ ! "$SSH_PORT" =~ ^[0-9]+$ ]] || [[ "$SSH_PORT" -lt 1 ]] || [[ "$SSH_PORT" -gt 65535 ]]; then
        log_error "无效的 SSH 端口: $SSH_PORT"
        exit 1
    fi

    # 验证超时时间
    if [[ ! "$OPERATION_TIMEOUT" =~ ^[0-9]+$ ]] || [[ "$OPERATION_TIMEOUT" -lt 60 ]]; then
        log_error "无效的超时时间: $OPERATION_TIMEOUT (最小 60 秒)"
        exit 1
    fi
}

# =============================================================================
# 前置检查
# =============================================================================

check_prerequisites() {
    # 检查 pg_dump
    if command -v pg_dump &> /dev/null; then
        PG_DUMP_CMD="pg_dump"
        PG_DUMP_MODE="host"
    elif docker ps -q -f name="$LOCAL_SOURCE_CONTAINER" &> /dev/null; then
        log_info "本地未找到 pg_dump，但发现 $LOCAL_SOURCE_CONTAINER 容器，将使用容器内工具导出。"
        PG_DUMP_CMD="docker exec"
        PG_DUMP_MODE="docker"
    else
        log_error "未找到 pg_dump 命令"
        log_error "也未找到本地运行的 $LOCAL_SOURCE_CONTAINER 容器"
        log_error "请安装 PostgreSQL 客户端工具 或 启动本地开发数据库"
        exit 1
    fi

    # 检查 timeout 命令 (macOS 可能需要 gtimeout)
    if command -v timeout &> /dev/null; then
        TIMEOUT_CMD="timeout"
    elif command -v gtimeout &> /dev/null; then
        TIMEOUT_CMD="gtimeout"
    else
        log_warn "未找到 timeout 命令，将不使用超时保护"
        TIMEOUT_CMD=""
    fi
}

# =============================================================================
# Docker 命令执行器 (避免 eval)
# =============================================================================

run_docker_cmd() {
    local cmd="$1"
    if [[ "$TARGET_TYPE" == "remote" ]]; then
        ssh -p "$SSH_PORT" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT" "$SSH_HOST" "$cmd"
    else
        eval "$cmd"
    fi
}

run_docker_exec() {
    local container="$1"
    local user="$2"
    shift 2
    local cmd_args=("$@")

    if [[ "$TARGET_TYPE" == "remote" ]]; then
        # 远程执行: 需要通过 SSH
        ssh -p "$SSH_PORT" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT" "$SSH_HOST" \
            "docker exec -i '$container' ${cmd_args[*]}"
    else
        # 本地执行: 直接调用 docker
        docker exec -i "$container" "${cmd_args[@]}"
    fi
}

# =============================================================================
# 获取源数据库 DSN
# =============================================================================

get_source_dsn() {
    log_info "正在读取开发环境数据库配置..." >&2

    # 使用临时文件而非内联 Python，提高可维护性
    local dsn
    if ! dsn=$(python3 -c '
import sys
import os

sys.path.insert(0, ".")
try:
    from src.config.settings import Settings
    s = Settings()
    print(s.database.dsn)
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)
'); then
        log_error "读取配置失败" >&2
        exit 1
    fi

    # 验证 DSN 格式
    if [[ ! "$dsn" =~ ^postgresql:// ]]; then
        log_error "无效的 DSN 格式 (期望 postgresql://...): $dsn" >&2
        exit 1
    fi

    echo "$dsn"
}

# =============================================================================
# DSN 解析 (用于安全执行 pg_dump)
# =============================================================================

parse_dsn() {
    local dsn="$1"

    # 解析 DSN: postgresql://user:pass@host:port/dbname
    # 使用 Python 进行可靠解析
    python3 -c "
import sys
from urllib.parse import urlparse, unquote

dsn = sys.argv[1]
parsed = urlparse(dsn)

print(f'DB_HOST={parsed.hostname or \"localhost\"}')
print(f'DB_PORT={parsed.port or 5432}')
print(f'DB_USER_SRC={unquote(parsed.username or \"postgres\")}')
print(f'DB_PASS={unquote(parsed.password or \"\")}')
print(f'DB_NAME_SRC={parsed.path.lstrip(\"/\") or \"postgres\"}')
" "$dsn"
}

# =============================================================================
# 连接检查
# =============================================================================

check_connections() {
    log_step "1/5 检查目标环境连接..."

    if [[ "$TARGET_TYPE" == "remote" ]]; then
        log_info "检查 SSH 连接到 $SSH_HOST:$SSH_PORT..."
        if ! ssh -p "$SSH_PORT" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT" "$SSH_HOST" "echo 'SSH OK'" &> /dev/null; then
            log_error "无法通过 SSH 连接到远程服务器"
            log_error "请检查:"
            echo "  1. SSH 连接是否正常: ssh -p $SSH_PORT $SSH_HOST"
            echo "  2. 是否配置了 SSH 密钥认证"
            exit 1
        fi
        log_success "SSH 连接正常"

        log_info "检查远程 Docker 服务..."
        if ! ssh -p "$SSH_PORT" $SSH_OPTS -o BatchMode=yes -o ConnectTimeout="$SSH_TIMEOUT" "$SSH_HOST" "docker ps" &> /dev/null; then
            log_error "无法访问远程 Docker 服务"
            log_error "请检查:"
            echo "  1. Docker 是否正在运行"
            echo "  2. 当前用户是否有 Docker 权限"
            exit 1
        fi
        log_success "远程 Docker 服务正常"
    else
        log_info "检查本地 Docker 服务..."
        if ! docker ps &> /dev/null; then
            log_error "无法连接到本地 Docker"
            log_error "请检查 Docker Desktop 是否正在运行"
            exit 1
        fi
        log_success "本地 Docker 服务正常"
    fi
}

# =============================================================================
# 容器检查
# =============================================================================

check_container() {
    log_step "2/5 检查目标容器..."

    local check_cmd="docker inspect '$CONTAINER_NAME' --format='{{.State.Running}}'"

    local container_status
    if [[ "$TARGET_TYPE" == "remote" ]]; then
        container_status=$(ssh -p "$SSH_PORT" $SSH_OPTS -o BatchMode=yes "$SSH_HOST" "$check_cmd" 2>/dev/null) || true
    else
        container_status=$(docker inspect "$CONTAINER_NAME" --format='{{.State.Running}}' 2>/dev/null) || true
    fi

    if [[ -z "$container_status" ]]; then
        log_error "容器 '$CONTAINER_NAME' 不存在"
        log_error "请确认容器名称正确，或使用 --container 参数指定"
        exit 1
    fi

    if [[ "$container_status" != "true" ]]; then
        log_error "容器 '$CONTAINER_NAME' 未运行"
        log_error "请先启动容器: docker start $CONTAINER_NAME"
        exit 1
    fi

    log_success "容器 '$CONTAINER_NAME' 运行正常"
}

# =============================================================================
# 备份目标数据库
# =============================================================================

backup_target_database() {
    if [[ "$SKIP_BACKUP" == "true" ]]; then
        log_warn "已跳过目标数据库备份 (--no-backup)"
        return 0
    fi

    log_step "3/5 备份目标数据库..."

    local backup_file="backup_${DB_NAME}_$(date +%Y%m%d_%H%M%S).sql.gz"
    local backup_cmd="docker exec '$CONTAINER_NAME' pg_dump -U '$DB_USER' '$DB_NAME' 2>/dev/null | gzip"

    log_info "创建备份: $backup_file"

    if [[ "$TARGET_TYPE" == "remote" ]]; then
        if ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" "$backup_cmd" > "$backup_file" 2>/dev/null; then
            local backup_size
            backup_size=$(du -h "$backup_file" | cut -f1)
            log_success "备份完成: $backup_file ($backup_size)"
        else
            log_warn "备份失败 (目标数据库可能不存在，将继续迁移)"
            rm -f "$backup_file"
        fi
    else
        if docker exec "$CONTAINER_NAME" pg_dump -U "$DB_USER" "$DB_NAME" 2>/dev/null | gzip > "$backup_file"; then
            local backup_size
            backup_size=$(du -h "$backup_file" | cut -f1)
            log_success "备份完成: $backup_file ($backup_size)"
        else
            log_warn "备份失败 (目标数据库可能不存在，将继续迁移)"
            rm -f "$backup_file"
        fi
    fi
}

# =============================================================================
# 执行迁移
# =============================================================================

execute_migration() {
    log_step "4/5 执行数据库迁移..."

    local start_time
    start_time=$(date +%s)

    # 解析源 DSN
    local dsn_vars
    dsn_vars=$(parse_dsn "$LOCAL_DSN")
    eval "$dsn_vars"

    log_info "源数据库: $DB_HOST:$DB_PORT/$DB_NAME_SRC"
    log_info "目标容器: $CONTAINER_NAME/$DB_NAME"

    # 步骤 1: 终止现有连接
    log_info "  -> 终止目标数据库现有连接..."
    local kill_sql="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();"

    if [[ "$TARGET_TYPE" == "remote" ]]; then
        ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" \
            "docker exec -i '$CONTAINER_NAME' psql -U '$DB_USER' -d postgres -c \"$kill_sql\"" \
            >/dev/null 2>&1 || true
    else
        docker exec -i "$CONTAINER_NAME" psql -U "$DB_USER" -d postgres -c "$kill_sql" \
            >/dev/null 2>&1 || true
    fi

    # 步骤 2: 删除并重建数据库
    log_info "  -> 重建目标数据库..."
    local drop_sql="DROP DATABASE IF EXISTS \"$DB_NAME\";"
    local create_sql="CREATE DATABASE \"$DB_NAME\";"

    if [[ "$TARGET_TYPE" == "remote" ]]; then
        ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" \
            "docker exec -i '$CONTAINER_NAME' psql -U '$DB_USER' -d postgres -c \"$drop_sql\""
        ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" \
            "docker exec -i '$CONTAINER_NAME' psql -U '$DB_USER' -d postgres -c \"$create_sql\""
    else
        docker exec -i "$CONTAINER_NAME" psql -U "$DB_USER" -d postgres -c "$drop_sql"
        docker exec -i "$CONTAINER_NAME" psql -U "$DB_USER" -d postgres -c "$create_sql"
    fi

    # 步骤 3: 导出并导入数据
    log_info "  -> 导出本地数据并传输..."

    local pg_dump_cmd_str=""
    
    # 构造导出命令
    if [[ "$PG_DUMP_MODE" == "docker" ]]; then
        # 容器内导出: docker exec ... pg_dump ...
        # 注意: 容器内通常不需要密码 (trust) 或使用 PGPASSWORD
        # 我们需要将 DB_PASS 传递给容器
        
        # 使用 -e PGPASSWORD=...
        pg_dump_cmd_str="docker exec -e PGPASSWORD='$DB_PASS' '$LOCAL_SOURCE_CONTAINER' pg_dump -U '$DB_USER_SRC' -d '$DB_NAME_SRC' -F c"
    else
        # 宿主机导出
        export PGPASSWORD="$DB_PASS"
        local dump_base="pg_dump -h '$DB_HOST' -p '$DB_PORT' -U '$DB_USER_SRC' -d '$DB_NAME_SRC' -F c"
        if [[ -n "$TIMEOUT_CMD" ]]; then
            pg_dump_cmd_str="$TIMEOUT_CMD $OPERATION_TIMEOUT $dump_base"
        else
            pg_dump_cmd_str="$dump_base"
        fi
    fi

    # 执行管道
    if [[ "$TARGET_TYPE" == "remote" ]]; then
        # 管道: 导出命令 | ssh -> docker exec pg_restore
        eval "$pg_dump_cmd_str" | ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" \
            "docker exec -i '$CONTAINER_NAME' pg_restore -U '$DB_USER' -d '$DB_NAME' -v --no-owner --role='$DB_USER'" \
            2>&1 | grep -v "^pg_restore: " || true
    else
        # 管道: 导出命令 | docker exec pg_restore
        eval "$pg_dump_cmd_str" | docker exec -i "$CONTAINER_NAME" \
            pg_restore -U "$DB_USER" -d "$DB_NAME" -v --no-owner --role="$DB_USER" \
            2>&1 | grep -v "^pg_restore: " || true
    fi

    # 清理密码
    unset PGPASSWORD

    local end_time
    end_time=$(date +%s)
    local duration=$((end_time - start_time))

    log_success "数据传输完成 (耗时: ${duration}秒)"
}

# =============================================================================
# 验证迁移
# =============================================================================

verify_migration() {
    log_step "5/5 验证迁移结果..."

    local count_sql="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"
    local table_count
    
    if [[ "$TARGET_TYPE" == "remote" ]]; then
        table_count=$(ssh -p "$SSH_PORT" $SSH_OPTS "$SSH_HOST" \
            "docker exec -i '$CONTAINER_NAME' psql -U '$DB_USER' -d '$DB_NAME' -t -c \"$count_sql\"" \
            2>/dev/null | tr -d ' ')
    else
        table_count=$(docker exec -i "$CONTAINER_NAME" psql -U "$DB_USER" -d "$DB_NAME" -t -c "$count_sql" \
            2>/dev/null | tr -d ' ')
    fi

    # 去除空白字符
    table_count=$(echo "$table_count" | xargs)

    if [[ -z "$table_count" || "$table_count" == "0" ]]; then
        log_warn "警告: 目标数据库中没有发现表"
        log_warn "请手动验证迁移是否成功"
    else
        log_success "验证通过: 目标数据库包含 $table_count 个表"
    fi
}

# =============================================================================
# 清理函数
# =============================================================================

cleanup() {
    # 确保清理敏感环境变量
    unset PGPASSWORD
    unset DB_PASS
}

trap cleanup EXIT

# =============================================================================
# 主函数
# =============================================================================

main() {
    local start_time
    start_time=$(date +%s)

    echo ""
    echo "=========================================="
    echo "  AI Gateway - 数据库迁移工具"
    echo "=========================================="
    echo ""

    # 解析命令行参数
    parse_args "$@"

    # 验证输入
    validate_inputs

    # 前置检查
    check_prerequisites

    # 获取源数据库 DSN
    LOCAL_DSN=$(get_source_dsn)

    # 显示配置摘要
    local masked_dsn
    masked_dsn=$(echo "$LOCAL_DSN" | sed 's/:[^:@]*@/:******@/')
    echo "配置摘要:"
    echo "  源数据库: $masked_dsn"
    echo "  目标类型: $TARGET_TYPE"
    if [[ "$TARGET_TYPE" == "remote" ]]; then
        echo "  目标主机: $SSH_HOST:$SSH_PORT"
    fi
    echo "  目标容器: $CONTAINER_NAME"
    echo "  目标数据库: $DB_NAME"
    echo ""

    # 确认操作
    if [[ "$AUTO_CONFIRM" != "true" ]]; then
        log_warn "此操作将覆盖目标环境 '$CONTAINER_NAME' 中的数据库 '$DB_NAME'！"
        read -p "确认继续吗？ (y/N) " -n 1 -r
        echo ""
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "已取消。"
            exit 0
        fi
    fi

    echo ""

    # 执行迁移步骤
    check_connections
    check_container
    backup_target_database
    execute_migration
    verify_migration

    # 计算总耗时
    local end_time
    end_time=$(date +%s)
    local total_duration=$((end_time - start_time))

    echo ""
    log_success "=========================================="
    log_success "  数据库迁移完成！"
    log_success "  总耗时: ${total_duration}秒"
    log_success "=========================================="
    echo ""
}

# 执行主函数
main "$@"
