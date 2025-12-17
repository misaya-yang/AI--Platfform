"""
LangGraph Server 代理层

负责：
- 完整代理 LangGraph Server 的 Assistant/Thread/Run API
- 负载均衡：支持多个 LangGraph Server 实例
- 限流检查：按用户层级应用不同限流策略
- 权限验证：验证 Thread 所有权、Assistant 访问权限
- 上下文注入：为 Run 注入用户上下文
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx


# ============ 数据类定义 ============

@dataclass
class UserContext:
    """用户上下文"""
    user_id: str
    tenant_id: str = ""
    tier: str = "anonymous"  # anonymous | normal | premium | enterprise | admin
    is_authenticated: bool = False
    ip: str = ""


@dataclass
class LangGraphInstance:
    """LangGraph Server 实例"""
    instance_id: str
    url: str                      # http://langgraph-1:8000
    weight: int = 1               # 权重
    is_healthy: bool = True
    active_connections: int = 0
    avg_latency: float = 0.0
    last_health_check: Optional[datetime] = None
    
    def __post_init__(self):
        self.url = self.url.rstrip("/")


@dataclass
class LoadBalancerConfig:
    """负载均衡配置"""
    strategy: str = "round_robin"  # round_robin | least_connections | weighted | latency_based
    health_check_interval: int = 10
    instances: List[LangGraphInstance] = field(default_factory=list)


# ============ 异常定义 ============

class LangGraphProxyError(Exception):
    """LangGraph 代理错误基类"""
    pass


class NoHealthyInstanceError(LangGraphProxyError):
    """没有健康的实例可用"""
    def __init__(self):
        super().__init__("No healthy LangGraph instances available")


class ForbiddenError(LangGraphProxyError):
    """权限拒绝"""
    def __init__(self, message: str = "Access denied"):
        super().__init__(message)


class QuotaExceededError(LangGraphProxyError):
    """配额超限"""
    def __init__(self, message: str):
        super().__init__(message)


class ThreadNotFoundError(LangGraphProxyError):
    """Thread 不存在"""
    def __init__(self, thread_id: str):
        super().__init__(f"Thread not found: {thread_id}")


# ============ 负载均衡器 ============

class LangGraphLoadBalancer:
    """LangGraph 服务负载均衡器"""
    
    def __init__(self, config: LoadBalancerConfig):
        self.strategy = config.strategy
        self.instances: List[LangGraphInstance] = config.instances
        self._rr_index = -1
    
    def add_instance(self, instance: LangGraphInstance) -> None:
        """添加实例"""
        self.instances.append(instance)
    
    def remove_instance(self, instance_id: str) -> None:
        """移除实例"""
        self.instances = [i for i in self.instances if i.instance_id != instance_id]
    
    def mark_unhealthy(self, instance_id: str) -> None:
        """标记实例不健康"""
        for inst in self.instances:
            if inst.instance_id == instance_id:
                inst.is_healthy = False
                break
    
    def mark_healthy(self, instance_id: str) -> None:
        """标记实例健康"""
        for inst in self.instances:
            if inst.instance_id == instance_id:
                inst.is_healthy = True
                inst.last_health_check = datetime.utcnow()
                break
    
    async def select_instance(self, request_type: str = None) -> LangGraphInstance:
        """选择目标实例"""
        healthy = [i for i in self.instances if i.is_healthy]
        
        if not healthy:
            raise NoHealthyInstanceError()
        
        if self.strategy == "round_robin":
            return self._round_robin(healthy)
        elif self.strategy == "least_connections":
            return self._least_connections(healthy)
        elif self.strategy == "weighted":
            return self._weighted(healthy)
        elif self.strategy == "latency_based":
            return self._latency_based(healthy)
        else:
            return self._round_robin(healthy)
    
    def _round_robin(self, instances: List[LangGraphInstance]) -> LangGraphInstance:
        """轮询"""
        self._rr_index = (self._rr_index + 1) % len(instances)
        return instances[self._rr_index]
    
    def _least_connections(self, instances: List[LangGraphInstance]) -> LangGraphInstance:
        """最少连接"""
        return min(instances, key=lambda i: i.active_connections)
    
    def _weighted(self, instances: List[LangGraphInstance]) -> LangGraphInstance:
        """加权（根据实例配置的权重）"""
        total_weight = sum(i.weight for i in instances)
        r = random.uniform(0, total_weight)
        cumulative = 0
        for instance in instances:
            cumulative += instance.weight
            if r <= cumulative:
                return instance
        return instances[-1]
    
    def _latency_based(self, instances: List[LangGraphInstance]) -> LangGraphInstance:
        """基于延迟（选择响应最快的）"""
        return min(instances, key=lambda i: i.avg_latency)


# ============ LangGraph 代理类 ============

class LangGraphProxy:
    """
    LangGraph Server 代理层
    负责：负载均衡、限流检查、请求增强、响应处理
    """
    
    # 用户层级对应的配额
    THREAD_QUOTAS = {
        "anonymous": 1,
        "normal": 10,
        "premium": 100,
        "enterprise": float('inf'),
        "admin": float('inf'),
    }
    
    def __init__(
        self,
        load_balancer: LangGraphLoadBalancer,
        rate_limiter: Optional[Any] = None,
        redis_client: Optional[Any] = None,
    ):
        self.lb = load_balancer
        self.rate_limiter = rate_limiter
        self.redis = redis_client
        self._clients: Dict[str, httpx.AsyncClient] = {}
    
    async def _get_client(self, instance: LangGraphInstance) -> httpx.AsyncClient:
        """获取或创建实例的 HTTP 客户端"""
        if instance.instance_id not in self._clients:
            self._clients[instance.instance_id] = httpx.AsyncClient(
                base_url=instance.url,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0),
            )
        return self._clients[instance.instance_id]
    
    async def close(self) -> None:
        """关闭所有客户端连接"""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
    
    # ============ Assistants API ============
    
    async def list_assistants(
        self,
        user: UserContext,
        limit: int = 10,
        offset: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """列出 Assistants"""
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"limit": limit, "offset": offset}
        if metadata:
            params["metadata"] = json.dumps(metadata)
        
        response = await client.post("/assistants/search", json=params)
        response.raise_for_status()
        return response.json()
    
    async def get_assistant(self, user: UserContext, assistant_id: str) -> Dict[str, Any]:
        """获取 Assistant 详情"""
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        response = await client.get(f"/assistants/{assistant_id}")
        response.raise_for_status()
        return response.json()
    
    async def create_assistant(
        self,
        user: UserContext,
        graph_id: str,
        config: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建自定义 Assistant（需要企业版权限）"""
        # 权限检查
        if user.tier not in ["enterprise", "admin"]:
            raise ForbiddenError("Custom assistants require enterprise plan")
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        payload: Dict[str, Any] = {"graph_id": graph_id}
        if config:
            payload["config"] = config
        if name:
            payload["name"] = name
        
        # 注入创建者信息
        payload["metadata"] = metadata or {}
        payload["metadata"]["created_by"] = user.user_id
        payload["metadata"]["tenant_id"] = user.tenant_id
        
        response = await client.post("/assistants", json=payload)
        response.raise_for_status()
        return response.json()
    
    # ============ Threads API ============
    
    async def create_thread(
        self,
        user: UserContext,
        metadata: Optional[Dict[str, Any]] = None,
        if_exists: str = "do_nothing",
    ) -> Dict[str, Any]:
        """创建 Thread，注入用户元数据"""
        
        # 检查 Thread 配额
        await self._check_thread_quota(user)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        # 注入用户元数据
        thread_metadata = metadata or {}
        thread_metadata.update({
            "owner_id": user.user_id,
            "tenant_id": user.tenant_id,
            "user_tier": user.tier,
            "created_at": datetime.utcnow().isoformat(),
        })
        
        payload = {
            "metadata": thread_metadata,
            "if_exists": if_exists,
        }
        
        response = await client.post("/threads", json=payload)
        response.raise_for_status()
        thread = response.json()
        
        # 记录用户的 Thread 计数
        await self._increment_thread_count(user)
        
        return thread
    
    async def get_thread(self, user: UserContext, thread_id: str) -> Dict[str, Any]:
        """获取 Thread，验证所有权"""
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        response = await client.get(f"/threads/{thread_id}")
        if response.status_code == 404:
            raise ThreadNotFoundError(thread_id)
        response.raise_for_status()
        
        thread = response.json()
        
        # 验证所有权
        self._verify_ownership(thread, user)
        
        return thread
    
    async def update_thread(
        self,
        user: UserContext,
        thread_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """更新 Thread"""
        # 先验证所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        payload = {}
        if metadata:
            payload["metadata"] = metadata
        
        response = await client.patch(f"/threads/{thread_id}", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def delete_thread(self, user: UserContext, thread_id: str) -> None:
        """删除 Thread"""
        # 先验证所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        response = await client.delete(f"/threads/{thread_id}")
        response.raise_for_status()
        
        # 减少 Thread 计数
        await self._decrement_thread_count(user)
    
    async def search_threads(
        self,
        user: UserContext,
        metadata: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """搜索用户的 Threads"""
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        # 只搜索用户自己的 Threads
        search_metadata = metadata or {}
        if user.tier != "admin":
            search_metadata["owner_id"] = user.user_id
        
        payload = {
            "metadata": search_metadata,
            "limit": limit,
            "offset": offset,
        }
        
        response = await client.post("/threads/search", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def get_thread_state(self, user: UserContext, thread_id: str) -> Dict[str, Any]:
        """获取 Thread 状态"""
        # 先验证所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        response = await client.get(f"/threads/{thread_id}/state")
        response.raise_for_status()
        return response.json()
    
    async def update_thread_state(
        self,
        user: UserContext,
        thread_id: str,
        values: Dict[str, Any],
        as_node: Optional[str] = None,
    ) -> Dict[str, Any]:
        """更新 Thread 状态"""
        # 先验证所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        payload: Dict[str, Any] = {"values": values}
        if as_node:
            payload["as_node"] = as_node
        
        response = await client.post(f"/threads/{thread_id}/state", json=payload)
        response.raise_for_status()
        return response.json()
    
    async def get_thread_history(
        self,
        user: UserContext,
        thread_id: str,
        limit: int = 10,
        before: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """获取 Thread 历史"""
        # 先验证所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"limit": limit}
        if before:
            params["before"] = before
        
        response = await client.get(f"/threads/{thread_id}/history", params=params)
        response.raise_for_status()
        return response.json()
    
    # ============ Runs API ============
    
    async def create_run(
        self,
        user: UserContext,
        thread_id: str,
        assistant_id: str,
        input_data: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        webhook: Optional[str] = None,
        interrupt_before: Optional[List[str]] = None,
        interrupt_after: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """创建 Run"""
        
        # 验证 Thread 所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        instance.active_connections += 1
        
        try:
            client = await self._get_client(instance)
            
            # 构建 Run 配置，注入用户上下文
            run_config = self._build_run_config(user, config)
            
            payload: Dict[str, Any] = {
                "assistant_id": assistant_id,
                "input": input_data,
                "config": run_config,
            }
            
            if metadata:
                payload["metadata"] = metadata
            if webhook:
                payload["webhook"] = webhook
            if interrupt_before:
                payload["interrupt_before"] = interrupt_before
            if interrupt_after:
                payload["interrupt_after"] = interrupt_after
            
            response = await client.post(f"/threads/{thread_id}/runs", json=payload)
            response.raise_for_status()
            return response.json()
        finally:
            instance.active_connections -= 1
    
    async def create_run_wait(
        self,
        user: UserContext,
        thread_id: str,
        assistant_id: str,
        input_data: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """创建 Run 并等待完成"""
        
        # 验证 Thread 所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        instance.active_connections += 1
        
        try:
            client = await self._get_client(instance)
            run_config = self._build_run_config(user, config)
            
            payload: Dict[str, Any] = {
                "assistant_id": assistant_id,
                "input": input_data,
                "config": run_config,
            }
            if metadata:
                payload["metadata"] = metadata
            
            response = await client.post(f"/threads/{thread_id}/runs/wait", json=payload)
            response.raise_for_status()
            return response.json()
        finally:
            instance.active_connections -= 1
    
    async def stream_run(
        self,
        user: UserContext,
        thread_id: Optional[str],
        assistant_id: str,
        input_data: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        stream_mode: Optional[List[str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式执行 Run"""
        
        # 如果有 thread_id，验证所有权
        if thread_id:
            await self.get_thread(user, thread_id)
            endpoint = f"/threads/{thread_id}/runs/stream"
        else:
            endpoint = "/runs/stream"
        
        instance = await self.lb.select_instance()
        instance.active_connections += 1
        
        try:
            client = await self._get_client(instance)
            run_config = self._build_run_config(user, config)
            
            payload: Dict[str, Any] = {
                "assistant_id": assistant_id,
                "input": input_data,
                "config": run_config,
                "stream_mode": stream_mode or ["messages", "updates"],
            }
            
            async with client.stream("POST", endpoint, json=payload) as response:
                response.raise_for_status()
                
                current_event = ""
                async for line in response.aiter_lines():
                    if not line:
                        current_event = ""
                        continue
                    
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                        continue
                    
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str and data_str != "[DONE]":
                            try:
                                data = json.loads(data_str)
                                yield {"event": current_event, "data": data}
                            except json.JSONDecodeError:
                                continue
        finally:
            instance.active_connections -= 1
    
    async def get_run(
        self,
        user: UserContext,
        thread_id: str,
        run_id: str,
    ) -> Dict[str, Any]:
        """获取 Run 详情"""
        # 验证 Thread 所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        response = await client.get(f"/threads/{thread_id}/runs/{run_id}")
        response.raise_for_status()
        return response.json()
    
    async def list_runs(
        self,
        user: UserContext,
        thread_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出 Thread 的 Runs"""
        # 验证 Thread 所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"limit": limit, "offset": offset}
        response = await client.get(f"/threads/{thread_id}/runs", params=params)
        response.raise_for_status()
        return response.json()
    
    async def cancel_run(
        self,
        user: UserContext,
        thread_id: str,
        run_id: str,
        wait: bool = False,
    ) -> None:
        """取消 Run"""
        # 验证 Thread 所有权
        await self.get_thread(user, thread_id)
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"wait": wait}
        response = await client.post(f"/threads/{thread_id}/runs/{run_id}/cancel", params=params)
        response.raise_for_status()
    
    # ============ Threadless Runs ============
    
    async def create_stateless_run(
        self,
        user: UserContext,
        assistant_id: str,
        input_data: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """创建无状态 Run（一次性对话）"""
        instance = await self.lb.select_instance()
        instance.active_connections += 1
        
        try:
            client = await self._get_client(instance)
            run_config = self._build_run_config(user, config)
            
            payload: Dict[str, Any] = {
                "assistant_id": assistant_id,
                "input": input_data,
                "config": run_config,
            }
            
            response = await client.post("/runs/wait", json=payload)
            response.raise_for_status()
            return response.json()
        finally:
            instance.active_connections -= 1
    
    # ============ Store API (Memory) ============
    
    async def store_get(
        self,
        user: UserContext,
        namespace: List[str],
        key: str,
    ) -> Optional[Dict[str, Any]]:
        """获取记忆，验证 namespace 权限"""
        
        if not self._validate_namespace_access(namespace, user):
            raise ForbiddenError("Access denied to this namespace")
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"namespace": json.dumps(namespace), "key": key}
        response = await client.get("/store/items", params=params)
        
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    
    async def store_put(
        self,
        user: UserContext,
        namespace: List[str],
        key: str,
        value: Dict[str, Any],
    ) -> None:
        """写入记忆，验证权限"""
        
        if not self._validate_namespace_access(namespace, user, write=True):
            raise ForbiddenError("Access denied to this namespace")
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        payload = {
            "namespace": namespace,
            "key": key,
            "value": value,
        }
        
        response = await client.post("/store/items", json=payload)
        response.raise_for_status()
    
    async def store_delete(
        self,
        user: UserContext,
        namespace: List[str],
        key: str,
    ) -> None:
        """删除记忆项"""
        
        if not self._validate_namespace_access(namespace, user, write=True):
            raise ForbiddenError("Access denied to this namespace")
        
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        params = {"namespace": json.dumps(namespace), "key": key}
        response = await client.delete("/store/items", params=params)
        response.raise_for_status()
    
    async def store_list_namespaces(
        self,
        user: UserContext,
        prefix: Optional[List[str]] = None,
    ) -> List[List[str]]:
        """列出命名空间"""
        instance = await self.lb.select_instance()
        client = await self._get_client(instance)
        
        # 限制只能看到自己的命名空间
        if user.tier != "admin":
            prefix = ["user", user.user_id] + (prefix or [])
        
        params = {}
        if prefix:
            params["prefix"] = json.dumps(prefix)
        
        response = await client.get("/store/namespaces", params=params)
        response.raise_for_status()
        return response.json()
    
    # ============ 辅助方法 ============
    
    def _verify_ownership(self, thread: Dict[str, Any], user: UserContext) -> None:
        """验证 Thread 所有权"""
        metadata = thread.get("metadata", {})
        owner_id = metadata.get("owner_id")
        
        # 管理员可访问所有
        if user.tier == "admin":
            return
        
        # 同租户可访问（企业版）
        if user.tier == "enterprise" and metadata.get("tenant_id") == user.tenant_id:
            return
        
        # 所有者
        if owner_id == user.user_id:
            return
        
        raise ForbiddenError("Access denied to this thread")
    
    def _build_run_config(self, user: UserContext, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """构建 Run 配置，注入用户上下文"""
        run_config = config or {}
        run_config["configurable"] = run_config.get("configurable", {})
        
        # 注入用户信息（供 Agent 使用）
        run_config["configurable"]["user_id"] = user.user_id
        run_config["configurable"]["user_tier"] = user.tier
        run_config["configurable"]["tenant_id"] = user.tenant_id
        # Namespacing for checkpointing/memory where supported by the upstream runtime.
        run_config["configurable"]["checkpoint_ns"] = user.tenant_id or ""
        
        return run_config
    
    async def _check_thread_quota(self, user: UserContext) -> None:
        """检查 Thread 配额"""
        max_threads = self.THREAD_QUOTAS.get(user.tier, 1)
        
        if max_threads == float('inf'):
            return
        
        current = await self._count_user_threads(user.user_id)
        
        if current >= max_threads:
            raise QuotaExceededError(f"Thread quota exceeded: {current}/{int(max_threads)}")
    
    async def _count_user_threads(self, user_id: str) -> int:
        """统计用户的 Thread 数量"""
        if self.redis:
            count = await self.redis.get(f"thread_count:{user_id}")
            return int(count) if count else 0
        return 0
    
    async def _increment_thread_count(self, user: UserContext) -> None:
        """增加 Thread 计数"""
        if self.redis:
            await self.redis.incr(f"thread_count:{user.user_id}")
    
    async def _decrement_thread_count(self, user: UserContext) -> None:
        """减少 Thread 计数"""
        if self.redis:
            await self.redis.decr(f"thread_count:{user.user_id}")
    
    def _validate_namespace_access(
        self,
        namespace: List[str],
        user: UserContext,
        write: bool = False,
    ) -> bool:
        """验证用户是否有权访问该 namespace"""
        if not namespace:
            return False
        
        # 管理员可访问所有
        if user.tier == "admin":
            return True
        
        # 全局 namespace 所有人可读，但只有管理员可写
        if namespace[0] == "global":
            return not write
        
        # 用户 namespace 只能访问自己的
        if namespace[0] == "user":
            return len(namespace) > 1 and namespace[1] == user.user_id
        
        # 租户 namespace（企业版功能）
        if namespace[0] == "tenant":
            if user.tier not in ["premium", "enterprise", "admin"]:
                return False
            return len(namespace) > 1 and namespace[1] == user.tenant_id
        
        return False


# ============ 记忆命名空间注入器 ============

class MemoryNamespaceInjector:
    """
    在请求转发到 LangGraph 之前，注入用户相关的 namespace
    确保用户只能访问自己的记忆
    """
    
    @staticmethod
    def inject_user_namespace(
        namespace: List[str],
        user: UserContext,
    ) -> List[str]:
        """为 namespace 添加用户前缀"""
        if not namespace:
            return ["user", user.user_id]
        
        # 如果已经有 user 前缀，验证是否是本人
        if namespace[0] == "user":
            if len(namespace) > 1 and namespace[1] != user.user_id:
                raise ForbiddenError("Cannot access other user's namespace")
            return namespace
        
        # 如果是全局 namespace，不修改
        if namespace[0] == "global":
            return namespace
        
        # 其他情况，添加用户前缀
        return ["user", user.user_id] + namespace
    
    @staticmethod
    def validate_namespace_access(
        namespace: List[str],
        user: UserContext,
    ) -> bool:
        """验证用户是否有权访问该 namespace"""
        if not namespace:
            return False
        
        # 全局 namespace 所有人可读
        if namespace[0] == "global":
            return True
        
        # 用户 namespace 只能访问自己的
        if namespace[0] == "user":
            return len(namespace) > 1 and namespace[1] == user.user_id
        
        # 租户 namespace
        if namespace[0] == "tenant":
            return len(namespace) > 1 and namespace[1] == user.tenant_id
        
        return False





