#!/usr/bin/env python
"""
统一数据库迁移管理器

功能：
- 追踪已应用的迁移
- 支持按顺序执行迁移
- 自动回滚失败的迁移（单个迁移内）
- 支持查看迁移状态

使用方法：
    python database/migrate.py status        # 查看迁移状态
    python database/migrate.py migrate       # 执行所有待执行的迁移
    python database/migrate.py migrate 003   # 执行指定迁移
"""

import asyncio
import asyncpg
import os
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

# Add project root to path for settings import
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 迁移文件目录
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _get_dsn() -> str:
    """Get database DSN from environment or settings."""
    # 1) Explicit environment variable takes priority
    dsn = os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn

    # 2) Try loading from project Settings (auto-loads config/.env etc.)
    try:
        from src.config.settings import Settings
        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception:
        pass

    # 3) Fallback default (use 127.0.0.1 instead of localhost for Windows compatibility)
    return "postgresql://postgres:111111@127.0.0.1:5432/gateway"


def discover_migrations() -> List[Tuple[str, str, Path]]:
    """
    发现所有迁移文件
    
    Returns:
        List of (version, description, path)
    """
    if not MIGRATIONS_DIR.exists():
        return []
    
    migrations = []
    pattern = re.compile(r"^(\d{3})_(.+)\.sql$")
    
    for file_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = pattern.match(file_path.name)
        if match:
            version = match.group(1)
            description = match.group(2).replace("_", " ").title()
            migrations.append((version, description, file_path))
    
    return migrations


async def ensure_migration_table(conn: asyncpg.Connection) -> None:
    """确保迁移记录表存在"""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            version VARCHAR(10) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            checksum VARCHAR(64),
            execution_time_ms INTEGER
        )
    """)


async def get_applied_migrations(conn: asyncpg.Connection) -> List[str]:
    """获取已应用的迁移版本"""
    rows = await conn.fetch("""
        SELECT version FROM schema_migrations ORDER BY version
    """)
    return [row["version"] for row in rows]


async def record_migration(
    conn: asyncpg.Connection,
    version: str,
    name: str,
    checksum: str,
    execution_time_ms: int,
) -> None:
    """记录迁移执行"""
    await conn.execute("""
        INSERT INTO schema_migrations (version, name, checksum, execution_time_ms)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (version) DO UPDATE SET
            applied_at = NOW(),
            checksum = $3,
            execution_time_ms = $4
    """, version, name, checksum, execution_time_ms)


def compute_checksum(content: str) -> str:
    """计算 SQL 内容的校验和"""
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def run_migration_file(conn: asyncpg.Connection, file_path: Path) -> Tuple[bool, str, int]:
    """
    执行单个迁移文件
    
    Returns:
        (success, message, execution_time_ms)
    """
    import time
    
    content = file_path.read_text(encoding="utf-8")
    start_time = time.time()
    
    try:
        # 使用事务执行迁移
        async with conn.transaction():
            await conn.execute(content)
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        return True, "OK", execution_time_ms
        
    except asyncpg.exceptions.DuplicateTableError as e:
        return True, f"Table already exists (skipped): {e}", 0
    except asyncpg.exceptions.DuplicateColumnError as e:
        return True, f"Column already exists (skipped): {e}", 0
    except asyncpg.exceptions.DuplicateObjectError as e:
        return True, f"Object already exists (skipped): {e}", 0
    except Exception as e:
        return False, str(e), 0


async def show_status(conn: asyncpg.Connection) -> None:
    """显示迁移状态"""
    await ensure_migration_table(conn)
    
    applied = await get_applied_migrations(conn)
    migrations = discover_migrations()
    
    print("\n" + "=" * 60)
    print("Database Migration Status")
    print("=" * 60)
    
    for version, description, path in migrations:
        status = "✅ Applied" if version in applied else "⏳ Pending"
        print(f"  {version}  {status:12}  {description}")
    
    print("=" * 60)
    print(f"Total: {len(migrations)} migrations, {len(applied)} applied, {len(migrations) - len(applied)} pending")
    print()


async def run_migrations(
    conn: asyncpg.Connection,
    target_version: Optional[str] = None,
) -> None:
    """
    执行迁移
    
    Args:
        conn: 数据库连接
        target_version: 指定版本（可选）
    """
    await ensure_migration_table(conn)
    
    applied = await get_applied_migrations(conn)
    migrations = discover_migrations()
    
    pending = [
        (v, d, p) for v, d, p in migrations
        if v not in applied and (target_version is None or v == target_version)
    ]
    
    if not pending:
        if target_version:
            print(f"Migration {target_version} already applied or not found.")
        else:
            print("All migrations are up to date.")
        return
    
    print("\n" + "=" * 60)
    print(f"Running {len(pending)} migration(s)...")
    print("=" * 60 + "\n")
    
    for version, description, file_path in pending:
        print(f"🚀 Applying {version}: {description}")
        print(f"   File: {file_path.name}")
        
        content = file_path.read_text(encoding="utf-8")
        checksum = compute_checksum(content)
        
        success, message, execution_time_ms = await run_migration_file(conn, file_path)
        
        if success:
            await record_migration(conn, version, description, checksum, execution_time_ms)
            print(f"   ✅ Success ({execution_time_ms}ms)")
            if message != "OK":
                print(f"   ⚠️  {message}")
        else:
            print(f"   ❌ Failed: {message}")
            print("\n⛔ Migration aborted due to error.")
            return
        
        print()
    
    print("=" * 60)
    print("✅ All migrations completed successfully!")
    print("=" * 60 + "\n")


async def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("Usage: python database/migrate.py [status|migrate] [version]")
        print()
        print("Commands:")
        print("  status          Show migration status")
        print("  migrate         Run all pending migrations")
        print("  migrate 003     Run specific migration")
        sys.exit(1)
    
    command = sys.argv[1].lower()
    target_version = sys.argv[2] if len(sys.argv) > 2 else None
    
    dsn = _get_dsn()
    print(f"Database: {dsn.replace(':111111@', ':******@').split('@')[1] if '@' in dsn else dsn}")
    
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        sys.exit(1)
    
    try:
        if command == "status":
            await show_status(conn)
        elif command == "migrate":
            await run_migrations(conn, target_version)
        else:
            print(f"Unknown command: {command}")
            sys.exit(1)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

