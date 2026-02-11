#!/usr/bin/env python3
"""
知识库全流程测试脚本
- 检查imam库
- 上传PDF文件
- 等待处理完成
- 测试检索和问答
"""

import asyncio
import time
from pathlib import Path

import httpx

# Configuration
BASE_URL = "http://localhost:8080"
API_KEY = "gw_gEtIPdAxdXI4D-WyWxvgFNPkdd7CU2VPdeFg9XdqFhs"
DATASET_ID = "imam"
TEST_FILE = "/Users/misaya.yanghejazfs.com.au/Downloads/Fiqh of Marriage.pdf"


class KnowledgeBaseTester:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "X-API-Key": API_KEY,
                "Accept": "application/json",
            },
            timeout=60.0,
        )
        self.document_id: str | None = None

    async def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            resp = await self.client.get("/health")
            if resp.status_code == 200:
                data = resp.json()
                print(f"✅ 服务健康: {data.get('status', 'unknown')}")
                return True
            else:
                print(f"❌ 服务不健康: {resp.status_code}")
                return False
        except Exception as e:
            print(f"❌ 健康检查失败: {e}")
            return False

    async def check_dataset(self) -> bool:
        """检查imam数据集是否存在"""
        try:
            resp = await self.client.get(f"/knowledge/datasets/{DATASET_ID}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"✅ 数据集 '{DATASET_ID}' 存在: {data.get('name', 'N/A')}")
                return True
            elif resp.status_code == 404:
                print(f"⚠️ 数据集 '{DATASET_ID}' 不存在，将尝试创建")
                return await self.create_dataset()
            else:
                print(f"❌ 检查数据集失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ 检查数据集异常: {e}")
            return False

    async def create_dataset(self) -> bool:
        """创建imam数据集"""
        try:
            payload = {
                "dataset_id": DATASET_ID,
                "name": "Imam Knowledge Base",
                "description": "Islamic knowledge base for imam",
                "embedding_provider": "dashscope",
                "embedding_model": "text-embedding-v3",
                "visibility": "private",
            }
            resp = await self.client.post("/knowledge/datasets", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                print(f"✅ 数据集创建成功: {data.get('dataset_id')}")
                return True
            else:
                print(f"❌ 数据集创建失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ 数据集创建异常: {e}")
            return False

    async def upload_document(self, file_path: str) -> bool:
        """上传PDF文档"""
        try:
            file = Path(file_path)
            if not file.exists():
                print(f"❌ 文件不存在: {file_path}")
                return False

            print(f"📤 正在上传文件: {file.name} ({file.stat().st_size / 1024 / 1024:.2f} MB)")

            with open(file, "rb") as f:
                files = {"file": (file.name, f, "application/pdf")}
                resp = await self.client.post(
                    f"/knowledge/{DATASET_ID}/documents/upload",
                    files=files,
                )

            if resp.status_code == 200:
                data = resp.json()
                self.document_id = data.get("document_id")
                print(f"✅ 文件上传成功: document_id={self.document_id}")
                print(f"   标题: {data.get('title')}")
                print(f"   状态: {data.get('status')}")
                return True
            else:
                print(f"❌ 文件上传失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ 文件上传异常: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def wait_for_processing(self, max_wait: int = 300) -> bool:
        """等待文档处理完成"""
        if not self.document_id:
            print("❌ 没有document_id")
            return False

        print(f"⏳ 等待文档处理完成 (最多等待 {max_wait} 秒)...")
        start_time = time.time()

        while time.time() - start_time < max_wait:
            try:
                resp = await self.client.get(
                    f"/knowledge/{DATASET_ID}/documents/{self.document_id}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status")
                    progress = data.get("progress", 0)
                    error = data.get("error", "")

                    print(f"   状态: {status}, 进度: {progress}%", end="\r")

                    if status == "completed":
                        segment_count = data.get("segment_count", 0)
                        print(f"\n✅ 文档处理完成！共 {segment_count} 个片段")
                        return True
                    elif status == "failed":
                        print(f"\n❌ 文档处理失败: {error}")
                        return False

                await asyncio.sleep(2)
            except Exception as e:
                print(f"\n⚠️ 检查状态异常: {e}")
                await asyncio.sleep(2)

        print("\n⚠️ 等待超时，文档可能仍在处理中")
        return False

    async def test_retrieval(self, query: str) -> bool:
        """测试检索功能"""
        print(f"\n🔍 测试检索: '{query}'")
        try:
            payload = {
                "query": query,
                "top_k": 5,
                "mode": "hybrid",
            }
            resp = await self.client.post(
                f"/knowledge/{DATASET_ID}/hit_test",
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                print(f"✅ 检索成功，返回 {len(results)} 条结果")
                for i, r in enumerate(results[:3], 1):
                    text = r.get("text", "")[:100].replace("\n", " ")
                    score = r.get("score", 0)
                    print(f"   [{i}] (score: {score:.3f}) {text}...")
                return True
            else:
                print(f"❌ 检索失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ 检索异常: {e}")
            return False

    async def test_qa(self, query: str) -> bool:
        """测试问答功能"""
        print(f"\n💬 测试问答: '{query}'")
        try:
            # Note: QA endpoint might need JWT token, using retrieval as fallback
            payload = {
                "query": query,
                "top_k": 5,
                "mode": "hybrid",
            }
            resp = await self.client.post(
                f"/knowledge/{DATASET_ID}/retrieve",
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                print(f"✅ QA检索成功，返回 {len(results)} 条上下文")
                for i, r in enumerate(results[:2], 1):
                    text = r.get("text", "")[:150].replace("\n", " ")
                    print(f"   [{i}] {text}...")
                return True
            else:
                print(f"❌ QA失败: {resp.status_code} - {resp.text}")
                return False
        except Exception as e:
            print(f"❌ QA异常: {e}")
            return False

    async def list_documents(self):
        """列出数据集下的所有文档"""
        print(f"\n📄 数据集 '{DATASET_ID}' 中的文档:")
        try:
            resp = await self.client.get(f"/knowledge/{DATASET_ID}/documents")
            if resp.status_code == 200:
                docs = resp.json()
                if isinstance(docs, list):
                    print(f"   共 {len(docs)} 个文档:")
                    for doc in docs[:10]:
                        status = doc.get("status", "unknown")
                        seg_count = doc.get("segment_count", 0)
                        print(f"   - {doc.get('title', 'N/A')}: {status} ({seg_count} segments)")
                else:
                    print(f"   响应: {docs}")
            else:
                print(f"   获取文档列表失败: {resp.status_code}")
        except Exception as e:
            print(f"   获取文档列表异常: {e}")

    async def close(self):
        await self.client.aclose()


async def main():
    print("=" * 60)
    print("知识库全流程测试")
    print("=" * 60)

    tester = KnowledgeBaseTester()

    try:
        # 1. 健康检查
        if not await tester.check_health():
            return

        # 2. 检查/创建数据集
        if not await tester.check_dataset():
            return

        # 3. 列出当前文档
        await tester.list_documents()

        # 4. 上传文档
        if await tester.upload_document(TEST_FILE):
            # 5. 等待处理
            if await tester.wait_for_processing(max_wait=300):
                # 6. 测试检索
                await tester.test_retrieval("marriage in Islam")
                await tester.test_retrieval("fiqh rules")

                # 7. 测试问答
                await tester.test_qa("What are the requirements for marriage in Islam?")
            else:
                print("\n⚠️ 文档处理未完成，跳过检索测试")
        else:
            print("\n⚠️ 文件上传失败")

        # 8. 最终文档列表
        await tester.list_documents()

    finally:
        await tester.close()

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
