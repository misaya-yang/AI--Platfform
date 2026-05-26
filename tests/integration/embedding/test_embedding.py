#!/usr/bin/env python3
"""测试Embedding连接"""

import asyncio
import os
import sys

# 添加项目路径（当前在tests目录，需要引用上级目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def test_dashscope():
    """测试DashScope连接"""
    print("=" * 60)
    print("测试DashScope Embedding连接")
    print("=" * 60)

    try:
        from dashscope import TextEmbedding

        # 从环境变量获取API key
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            print("DASHSCOPE_API_KEY not set")
            return False

        print(f"API Key: {api_key[:20]}...")

        # 禁用SSL验证（测试用）
        import ssl

        ssl._create_default_https_context = ssl._create_unverified_context

        print("\n尝试调用embedding API...")

        resp = await asyncio.to_thread(
            TextEmbedding.call,
            model="text-embedding-v3",
            input=["测试文本 test text"],
            api_key=api_key,
        )

        print(f"响应状态: {resp.status_code}")

        if resp.status_code == 200:
            output = getattr(resp, "output", None)
            if output:
                # 检查是否是字典格式
                if isinstance(output, dict) and "embeddings" in output:
                    embeddings = output["embeddings"]
                    if embeddings and len(embeddings) > 0:
                        embedding = embeddings[0].get("embedding", [])
                        print(f"✅ 成功! 向量维度: {len(embedding)}")
                        return True
                # 检查是否有embeddings属性
                elif hasattr(output, "embeddings"):
                    vectors = [emb.embedding for emb in output.embeddings]
                    print(f"✅ 成功! 向量维度: {len(vectors[0]) if vectors else 0}")
                    return True

            print(f"❌ 响应格式错误: {output}")
            return False
        else:
            print(f"❌ API错误: {resp.code} - {resp.message}")
            return False

    except Exception as e:
        print(f"❌ 异常: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return False


async def test_gemini():
    """测试Gemini连接"""
    print("\n" + "=" * 60)
    print("测试Gemini Embedding连接")
    print("=" * 60)

    try:
        import httpx

        api_key = os.getenv("GOOGLE_API_KEY", "")
        if not api_key:
            print("⚠️ 未配置GOOGLE_API_KEY，跳过Gemini测试")
            return None

        print(f"API Key: {api_key[:20]}...")

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                params={"key": api_key},
                json={
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": "测试文本 test text"}]},
                    "task_type": "RETRIEVAL_DOCUMENT",
                },
            )

            print(f"响应状态: {resp.status_code}")

            if resp.status_code == 200:
                data = resp.json()
                if "embedding" in data:
                    vector = data["embedding"]["values"]
                    print(f"✅ 成功! 向量维度: {len(vector)}")
                    return True
                else:
                    print(f"❌ 响应格式错误: {data}")
                    return False
            else:
                print(f"❌ API错误: {resp.text}")
                return False

    except Exception as e:
        print(f"❌ 异常: {type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
        return False


async def main():
    # 测试DashScope
    dashscope_ok = await test_dashscope()

    # 测试Gemini
    gemini_ok = await test_gemini()

    print("\n" + "=" * 60)
    print("测试结果")
    print("=" * 60)
    print(f"DashScope: {'✅ 可用' if dashscope_ok else '❌ 不可用'}")
    if gemini_ok is not None:
        print(f"Gemini: {'✅ 可用' if gemini_ok else '❌ 不可用'}")
    print("=" * 60)

    # 建议
    if not dashscope_ok and gemini_ok:
        print("\n💡 建议：切换到Gemini embedding")
    elif not dashscope_ok and not gemini_ok:
        print("\n⚠️ 警告：两个embedding服务都不可用")


if __name__ == "__main__":
    asyncio.run(main())
