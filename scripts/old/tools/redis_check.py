#!/usr/bin/env python3
"""
Redis 连接检查脚本（非 pytest 用例）
"""
import asyncio
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import redis.asyncio as aioredis
except ImportError:
    print("错误: 请先安装 redis: pip install redis")
    sys.exit(1)


async def check_redis(url: str) -> bool:
    """测试 Redis 连接"""
    print(f"连接 Redis: {url.replace(':111111@', ':******@')}")
    
    try:
        client = await aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
        )
        
        # 测试 ping
        pong = await client.ping()
        print(f"[OK] Redis 连接成功 (ping: {pong})")
        
        # 获取 Redis 信息
        info = await client.info("server")
        print(f"  Redis 版本: {info.get('redis_version', 'unknown')}")
        print(f"  操作系统: {info.get('os', 'unknown')}")
        
        # 测试读写
        test_key = "gateway:test"
        await client.set(test_key, "hello", ex=10)
        value = await client.get(test_key)
        await client.delete(test_key)
        print(f"[OK] 读写测试成功 (value: {value})")
        
        await client.close()
        return True
        
    except Exception as e:
        print(f"[ERR] Redis 连接失败: {e}")
        print("\n请检查:")
        print("  1. Redis 容器是否正在运行")
        print("  2. 连接参数是否正确")
        print("  3. 网络是否可达")
        return False


async def main():
    url = os.environ.get(
        "GATEWAY_REDIS__URL",
        "redis://:111111@localhost:6379/0"
    )
    
    print("=" * 50)
    print("AI Gateway Redis 连接检查")
    print("=" * 50 + "\n")
    
    success = await check_redis(url)
    
    print("\n" + "=" * 50)
    if success:
        print("[OK] Redis 检查通过!")
    else:
        print("[ERR] Redis 检查失败")
        sys.exit(1)
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
