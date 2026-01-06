"""
测试 Hejaz Agent 服务

测试网关对 Hejaz Agent 服务的调用，包括：
- 同步调用 (invoke)
- 流式调用 (stream)
- 会话管理（多轮对话）
"""

import asyncio
import json
import os
import uuid
from typing import Optional

import httpx
import pytest


# 网关基础 URL
GATEWAY_BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://localhost:8080")
SERVICE_ID = os.getenv("HEJAZ_AGENT_SERVICE_ID", "Hejaz Agent")

# Only force these tests when explicitly requested.
RUN_HEJAZ_AGENT_TESTS = os.getenv("RUN_HEJAZ_AGENT_TESTS", "").lower() in {"1", "true", "yes"}


async def _skip_if_hejaz_unavailable(tester: "HejazAgentTester") -> None:
    """
    Skip Hejaz Agent integration tests unless explicitly enabled or service is available.
    """
    if RUN_HEJAZ_AGENT_TESTS:
        return
    if not await tester.test_health():
        pytest.skip("Hejaz Agent tests skipped: gateway not available")
    if not await tester.test_service_exists():
        pytest.skip("Hejaz Agent tests skipped: service not registered")


class HejazAgentTester:
    """Hejaz Agent 测试类"""
    
    def __init__(self, base_url: str = GATEWAY_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=60.0)
        self.session_id: Optional[str] = None
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()
    
    def generate_session_id(self) -> str:
        """生成 UUID 格式的会话 ID"""
        return str(uuid.uuid4())
    
    async def test_health(self) -> bool:
        """测试网关健康状态"""
        try:
            response = await self.client.get(f"{self.base_url}/api/v1/health")
            response.raise_for_status()
            print(f"✓ 网关健康检查通过: {response.json()}")
            return True
        except Exception as e:
            print(f"✗ 网关健康检查失败: {e}")
            return False
    
    async def test_service_exists(self) -> bool:
        """测试服务是否存在"""
        try:
            response = await self.client.get(f"{self.base_url}/api/v1/services/{SERVICE_ID}")
            response.raise_for_status()
            service_info = response.json()
            print(f"✓ 服务存在: {service_info.get('name', SERVICE_ID)}")
            print(f"  服务类型: {service_info.get('service_type', 'unknown')}")
            print(f"  会话支持: {service_info.get('session_enabled', False)}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"✗ 服务不存在: {SERVICE_ID}")
            else:
                print(f"✗ 查询服务失败: {e}")
            return False
    
    async def test_invoke(self, message: str, use_session: bool = False) -> Optional[dict]:
        """测试同步调用"""
        print(f"\n📤 同步调用测试: {message[:50]}...")
        
        payload = {
            "service_id": SERVICE_ID,
            "inputs": [{"type": "text", "data": message}],
        }
        
        if use_session:
            if not self.session_id:
                self.session_id = self.generate_session_id()
            payload["session_id"] = self.session_id
            print(f"  会话 ID: {self.session_id}")
        
        try:
            response = await self.client.post(
                f"{self.base_url}/api/v1/invoke",
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            
            print(f"✓ 调用成功")
            print(f"  请求 ID: {result.get('request_id')}")
            print(f"  状态: {result.get('status')}")
            
            # 提取输出内容
            outputs = result.get("outputs", [])
            if outputs:
                content = outputs[0].get("data", "")
                print(f"  响应内容: {content[:200]}...")
                if len(content) > 200:
                    print(f"  (共 {len(content)} 字符)")
            
            return result
        except httpx.HTTPStatusError as e:
            print(f"✗ 调用失败: {e.response.status_code}")
            print(f"  错误信息: {e.response.text}")
            return None
        except Exception as e:
            print(f"✗ 调用异常: {e}")
            return None
    
    async def test_stream(self, message: str, use_session: bool = False) -> Optional[str]:
        """测试流式调用"""
        print(f"\n📡 流式调用测试: {message[:50]}...")
        
        payload = {
            "service_id": SERVICE_ID,
            "inputs": [{"type": "text", "data": message}],
        }
        
        if use_session:
            if not self.session_id:
                self.session_id = self.generate_session_id()
            payload["session_id"] = self.session_id
            print(f"  会话 ID: {self.session_id}")
        
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url}/api/v1/stream",
                json=payload,
            ) as response:
                response.raise_for_status()
                
                print(f"✓ 流式连接建立")
                print(f"  状态码: {response.status_code}")
                
                accumulated_text = ""
                chunk_count = 0
                tool_calls = []
                
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    
                    # 解析 SSE 格式
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        
                        try:
                            chunk = json.loads(data_str)
                            chunk_count += 1
                            
                            event_type = chunk.get("event_type", "text_delta")
                            content = chunk.get("content", {})
                            content_data = content.get("data", "")
                            
                            # 处理文本增量
                            if event_type == "text_delta" and content_data:
                                accumulated_text += content_data
                                print(f"  [{chunk_count}] 文本增量: {content_data[:50]}...")
                            
                            # 处理工具调用
                            tool_call = chunk.get("tool_call")
                            if tool_call:
                                tool_name = tool_call.get("name", "")
                                if event_type == "tool_call_start":
                                    print(f"  [{chunk_count}] 🔧 工具调用开始: {tool_name}")
                                    tool_calls.append(tool_call)
                                elif event_type == "tool_call_delta":
                                    print(f"  [{chunk_count}] 🔧 工具调用增量: {tool_name}")
                                elif event_type == "tool_result":
                                    result = content_data
                                    print(f"  [{chunk_count}] ✅ 工具结果: {tool_name} -> {result[:50]}...")
                            
                            # 检查是否完成
                            if chunk.get("is_final"):
                                print(f"  [{chunk_count}] ✓ 流式响应完成")
                                break
                                
                        except json.JSONDecodeError as e:
                            print(f"  ⚠️  JSON 解析失败: {e}")
                            continue
                
                print(f"\n✓ 流式调用完成")
                print(f"  总 chunk 数: {chunk_count}")
                print(f"  累积文本长度: {len(accumulated_text)} 字符")
                print(f"  工具调用数: {len(tool_calls)}")
                
                if accumulated_text:
                    print(f"\n完整响应内容:")
                    print(f"  {accumulated_text[:500]}...")
                    if len(accumulated_text) > 500:
                        print(f"  (共 {len(accumulated_text)} 字符)")
                
                return accumulated_text
                
        except httpx.HTTPStatusError as e:
            print(f"✗ 流式调用失败: {e.response.status_code}")
            try:
                error_text = await e.response.aread()
                print(f"  错误信息: {error_text.decode()}")
            except:
                pass
            return None
        except Exception as e:
            print(f"✗ 流式调用异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    async def test_conversation(self):
        """测试多轮对话（会话管理）"""
        print(f"\n💬 多轮对话测试")
        
        # 创建新会话
        self.session_id = self.generate_session_id()
        print(f"  会话 ID: {self.session_id}")
        
        messages = [
            "你好，我想了解一下 Halal Money 的投资方式",
            "最低投资额是多少？",
            "可以通过哪些方式购买？",
        ]
        
        for i, message in enumerate(messages, 1):
            print(f"\n--- 第 {i} 轮对话 ---")
            result = await self.test_stream(message, use_session=True)
            if not result:
                print(f"✗ 第 {i} 轮对话失败")
                break
            
            # 等待一下再进行下一轮
            if i < len(messages):
                await asyncio.sleep(1)
        
        print(f"\n✓ 多轮对话测试完成")


# ============ pytest 测试用例 ============

@pytest.mark.asyncio
async def test_hejaz_agent_health():
    """测试网关和服务健康状态"""
    tester = HejazAgentTester()
    try:
        if not RUN_HEJAZ_AGENT_TESTS:
            await _skip_if_hejaz_unavailable(tester)
        assert await tester.test_health()
        assert await tester.test_service_exists()
    finally:
        await tester.close()


@pytest.mark.asyncio
async def test_hejaz_agent_invoke():
    """测试同步调用"""
    tester = HejazAgentTester()
    try:
        if not RUN_HEJAZ_AGENT_TESTS:
            await _skip_if_hejaz_unavailable(tester)
        result = await tester.test_invoke("你好，介绍一下 Halal Money")
        assert result is not None
        assert result.get("status") == "success"
        assert len(result.get("outputs", [])) > 0
    finally:
        await tester.close()


@pytest.mark.asyncio
async def test_hejaz_agent_stream():
    """测试流式调用"""
    tester = HejazAgentTester()
    try:
        if not RUN_HEJAZ_AGENT_TESTS:
            await _skip_if_hejaz_unavailable(tester)
        result = await tester.test_stream("请详细说明 Halal Money 的投资方式")
        assert result is not None
        assert len(result) > 0
    finally:
        await tester.close()


@pytest.mark.asyncio
async def test_hejaz_agent_conversation():
    """测试多轮对话"""
    tester = HejazAgentTester()
    try:
        if not RUN_HEJAZ_AGENT_TESTS:
            await _skip_if_hejaz_unavailable(tester)
        await tester.test_conversation()
    finally:
        await tester.close()


@pytest.mark.asyncio
async def test_hejaz_agent_stream_with_session():
    """测试带会话的流式调用"""
    tester = HejazAgentTester()
    try:
        if not RUN_HEJAZ_AGENT_TESTS:
            await _skip_if_hejaz_unavailable(tester)
        # 第一轮
        result1 = await tester.test_stream("什么是 Halal Money？", use_session=True)
        assert result1 is not None
        
        # 等待一下
        await asyncio.sleep(1)
        
        # 第二轮（应该能记住上下文）
        result2 = await tester.test_stream("最低投资额是多少？", use_session=True)
        assert result2 is not None
        
        print(f"\n✓ 会话上下文测试完成")
        print(f"  第一轮响应长度: {len(result1)}")
        print(f"  第二轮响应长度: {len(result2)}")
    finally:
        await tester.close()


# ============ 命令行直接运行 ============

async def main():
    """主函数，可以直接运行测试"""
    print("=" * 60)
    print("Hejaz Agent 服务测试")
    print("=" * 60)
    
    tester = HejazAgentTester()
    
    try:
        # 1. 健康检查
        print("\n[1/5] 健康检查")
        if not await tester.test_health():
            print("✗ 网关不可用，请先启动网关服务")
            return
        
        # 2. 服务检查
        print("\n[2/5] 服务检查")
        if not await tester.test_service_exists():
            print("✗ 服务不存在，请检查服务配置")
            return
        
        # 3. 同步调用测试
        print("\n[3/5] 同步调用测试")
        await tester.test_invoke("你好，介绍一下 Halal Money")
        
        # 4. 流式调用测试
        print("\n[4/5] 流式调用测试")
        await tester.test_stream("请详细说明 Halal Money 的投资方式")
        
        # 5. 多轮对话测试
        print("\n[5/5] 多轮对话测试")
        await tester.test_conversation()
        
        print("\n" + "=" * 60)
        print("✓ 所有测试完成")
        print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  测试被用户中断")
    except Exception as e:
        print(f"\n\n✗ 测试异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await tester.close()


if __name__ == "__main__":
    asyncio.run(main())

