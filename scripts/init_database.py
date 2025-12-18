#!/usr/bin/env python3
"""
数据库初始化脚本

用于初始化 PostgreSQL 数据库和创建表结构
"""
import asyncio
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    print("错误: 请先安装 asyncpg: pip install asyncpg")
    sys.exit(1)


def _get_dsn() -> str:
    # 1) 显式环境变量优先（便于临时覆盖）
    dsn = os.environ.get("GATEWAY_DATABASE__DSN")
    if dsn:
        return dsn

    # 2) 尝试读取项目 Settings（会自动加载 config/.env 等）
    try:
        from src.config.settings import Settings

        settings = Settings()
        if getattr(settings, "database", None) and settings.database.dsn:
            return settings.database.dsn
    except Exception:
        pass

    # 3) 兜底默认值
    return "postgresql://postgres:111111@localhost:5432/gateway"


async def create_database(dsn: str, db_name: str = "gateway"):
    """创建数据库（如果不存在）"""
    # 连接到默认的 postgres 数据库
    default_dsn = dsn.rsplit("/", 1)[0] + "/postgres"
    
    try:
        conn = await asyncpg.connect(default_dsn)
        
        # 检查数据库是否存在
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        
        if not exists:
            # 创建数据库
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"[OK] 数据库 '{db_name}' 创建成功")
        else:
            print(f"[OK] 数据库 '{db_name}' 已存在")
        
        await conn.close()
        return True
        
    except Exception as e:
        print(f"[ERROR] 创建数据库失败: {e}")
        return False


async def execute_schema(dsn: str, schema_path: str):
    """执行建表脚本"""
    try:
        # 读取 SQL 文件
        with open(schema_path, 'r', encoding='utf-8') as f:
            sql = f.read()
        
        # 连接数据库
        conn = await asyncpg.connect(dsn)
        
        # 执行 SQL
        await conn.execute(sql)
        
        await conn.close()
        print("[OK] 建表脚本执行成功")
        return True
        
    except Exception as e:
        print(f"[ERROR] 执行建表脚本失败: {e}")
        return False


async def test_connection(dsn: str):
    """测试数据库连接"""
    try:
        conn = await asyncpg.connect(dsn)
        version = await conn.fetchval("SELECT version()")
        await conn.close()
        print("[OK] 数据库连接成功")
        print(f"  PostgreSQL 版本: {version.split(',')[0]}")
        return True
    except Exception as e:
        print(f"[ERROR] 数据库连接失败: {e}")
        return False


async def list_tables(dsn: str):
    """列出所有表"""
    try:
        conn = await asyncpg.connect(dsn)
        tables = await conn.fetch("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            ORDER BY table_name
        """)
        await conn.close()
        
        if tables:
            print(f"\n数据库中的表 ({len(tables)} 个):")
            for t in tables:
                print(f"  - {t['table_name']}")
        else:
            print("\n数据库中没有表")
        
        return True
    except Exception as e:
        print(f"[ERROR] 查询表失败: {e}")
        return False


async def main():
    # 从环境变量或默认值获取配置
    dsn = _get_dsn()
    
    schema_path = project_root / "database" / "schema.sql"
    
    print("=" * 50)
    print("AI Gateway 数据库初始化")
    print("=" * 50)
    print(f"\n数据库连接: {dsn.replace(':111111@', ':******@')}")
    print(f"建表脚本: {schema_path}\n")
    
    # 解析数据库名
    db_name = dsn.rsplit("/", 1)[-1]
    base_dsn = dsn.rsplit("/", 1)[0]
    
    # 1. 创建数据库
    print("步骤 1: 创建数据库...")
    await create_database(dsn, db_name)
    
    # 2. 测试连接
    print("\n步骤 2: 测试数据库连接...")
    if not await test_connection(dsn):
        print("\n无法连接到数据库，请检查:")
        print("  1. PostgreSQL 容器是否正在运行")
        print("  2. 连接参数是否正确")
        print("  3. 网络是否可达")
        sys.exit(1)
    
    # 3. 执行建表脚本
    print("\n步骤 3: 执行建表脚本...")
    if not schema_path.exists():
        print(f"[ERROR] 找不到建表脚本: {schema_path}")
        sys.exit(1)
    
    await execute_schema(dsn, str(schema_path))
    
    # 4. 列出所有表
    print("\n步骤 4: 验证表结构...")
    await list_tables(dsn)
    
    print("\n" + "=" * 50)
    print("[OK] 数据库初始化完成!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
