你是一个专注于 API 网关和微服务架构的高级工程师。我需要你帮我完善 AI 服务网关平台，主要实现鉴权、限流、以及与 LangGraph 服务的安全对接。

## 项目背景
- 这是一个 AI 服务网关平台，负责统一管理 AI 服务的访问
- 当前已有 LangGraph 适配器，100% 对接了 LangGraph 的所有接口
- 下游是 LangGraph Agent 服务，已实现内置 Auth 系统
- 上游是各种应用端，会携带用户信息（登录用户或游客）

## 网关的核心职责
1. 鉴权：验证应用端的请求，提取用户信息
2. 限流：基于用户、IP、全局多维度限流
3. 路由：将请求路由到 LangGraph 服务
4. 适配：在请求中注入用户信息 header

## 关键约束
1. 网关负责真正的 token 验证（JWT 解析或调用用户服务）
2. 验证后将用户信息通过 header 透传给 LangGraph
3. LangGraph 的 Auth 系统信任这些 header（内网通信）
4. 限流使用 Redis 实现，支持滑动窗口

## 你需要完成的任务
请按照下面的 Plan 逐步实现，每完成一步请确认后再继续。
实施 Plan
Phase 1: 梳理现有适配器结构
目标：理解当前 LangGraph 适配器的结构，确定需要修改的位置
步骤：

列出适配器相关的所有文件
确认以下功能的实现位置：

HTTP 客户端配置
请求转发逻辑
响应处理逻辑
错误处理


确认中间件/拦截器的位置

输出：适配器结构文档，标注需要修改的文件

Phase 2: 实现鉴权中间件
目标：创建统一的鉴权中间件
逻辑说明：
python# 伪代码

class AuthMiddleware:
    """
    鉴权中间件
    
    职责：
    1. 从请求中提取认证信息（token 或其他）
    2. 验证认证信息的有效性
    3. 提取用户信息
    4. 将用户信息注入到请求上下文
    """
    
    async def __call__(self, request, call_next):
        # 1. 提取认证信息
        auth_header = request.headers.get("Authorization")
        guest_session = request.headers.get("X-Guest-Session")
        
        # 2. 验证并提取用户信息
        user_info = await self.authenticate(auth_header, guest_session)
        
        # 3. 注入到请求上下文
        request.state.user = user_info
        
        # 4. 继续处理
        return await call_next(request)
    
    async def authenticate(self, auth_header, guest_session):
        """
        认证逻辑
        
        场景1: 有 Authorization header (登录用户)
        - 解析 JWT token
        - 或调用用户服务验证
        - 返回 {user_id, user_type="user", is_authenticated=True, ...}
        
        场景2: 有 X-Guest-Session header (游客)
        - 验证 session 是否有效（可选）
        - 返回 {user_id=session_id, user_type="guest", is_authenticated=False, ...}
        
        场景3: 都没有
        - 返回 401 Unauthorized
        """
JWT 验证的两种方式：
python# 方式1: 本地验证（推荐，性能好）
def verify_jwt_local(token: str) -> dict:
    """
    使用公钥本地验证 JWT
    
    1. 解析 token header 获取 kid
    2. 从缓存或配置获取对应公钥
    3. 验证签名
    4. 检查过期时间
    5. 返回 payload
    """

# 方式2: 远程验证（灵活，可实时吊销）
async def verify_jwt_remote(token: str) -> dict:
    """
    调用用户服务验证 JWT
    
    1. POST /internal/verify-token {token}
    2. 返回用户信息或错误
    """

Phase 3: 实现限流中间件
目标：创建基于 Redis 的多维度限流
逻辑说明：
python# 伪代码

class RateLimitMiddleware:
    """
    限流中间件
    
    多维度限流策略：
    1. 全局限流 - 保护后端服务
    2. 用户级限流 - 防止单用户滥用
    3. IP 级限流 - 防止未登录滥用
    """
    
    def __init__(self, redis_client):
        self.redis = redis_client
        
        # 限流配置
        self.limits = {
            "global": {"limit": 1000, "window": 60},      # 1000 req/min 全局
            "user": {"limit": 30, "window": 60},          # 30 req/min 每用户
            "guest": {"limit": 10, "window": 60},         # 10 req/min 每游客
            "ip": {"limit": 60, "window": 60},            # 60 req/min 每IP
        }
    
    async def __call__(self, request, call_next):
        user_info = request.state.user  # 从鉴权中间件获取
        client_ip = self.get_client_ip(request)
        
        # 检查各维度限流
        checks = [
            ("global:ratelimit", self.limits["global"]),
            (f"user:{user_info['user_id']}:ratelimit", 
             self.limits["user"] if user_info["user_type"] == "user" else self.limits["guest"]),
            (f"ip:{client_ip}:ratelimit", self.limits["ip"]),
        ]
        
        for key, config in checks:
            if not await self.check_rate_limit(key, config):
                return Response(status_code=429, body={
                    "error": "Too Many Requests",
                    "retry_after": config["window"]
                })
        
        return await call_next(request)
    
    async def check_rate_limit(self, key: str, config: dict) -> bool:
        """
        滑动窗口限流算法
        
        使用 Redis INCR + EXPIRE 实现简单计数器
        或使用 Redis Sorted Set 实现精确滑动窗口
        
        返回 True 表示允许，False 表示限流
        """
滑动窗口实现（精确版）：
python# 伪代码

async def check_rate_limit_sliding_window(self, key: str, limit: int, window: int) -> bool:
    """
    使用 Redis Sorted Set 实现滑动窗口
    
    1. 当前时间戳作为 score
    2. 移除窗口外的记录
    3. 统计窗口内的请求数
    4. 如果未超限，添加当前请求
    """
    now = time.time()
    window_start = now - window
    
    pipe = self.redis.pipeline()
    pipe.zremrangebyscore(key, 0, window_start)  # 移除过期的
    pipe.zcard(key)                               # 统计当前数量
    pipe.zadd(key, {str(uuid4()): now})          # 添加当前请求
    pipe.expire(key, window)                      # 设置过期
    
    results = await pipe.execute()
    current_count = results[1]
    
    return current_count < limit

Phase 4: 修改 LangGraph 适配器 - 注入用户信息
目标：在转发请求到 LangGraph 时注入用户信息 header
逻辑说明：
python# 伪代码

class LangGraphAdapter:
    """
    LangGraph 适配器
    
    核心修改：在所有请求中注入用户信息 header
    """
    
    def _build_langgraph_headers(self, user_info: dict, original_headers: dict) -> dict:
        """
        构建发送给 LangGraph 的 headers
        
        1. 基础 headers（Content-Type 等）
        2. 注入用户信息 headers
        3. 可选：保留部分原始 headers
        """
        headers = {
            "Content-Type": "application/json",
            
            # 关键：注入用户信息
            "X-User-Id": user_info["user_id"],
            "X-User-Type": user_info["user_type"],  # "user" or "guest"
        }
        
        # 如果有原始 token，也可以透传（可选）
        if auth := original_headers.get("Authorization"):
            headers["Authorization"] = auth
        
        return headers
    
    async def forward_request(self, request, user_info: dict):
        """
        转发请求到 LangGraph
        
        1. 构建 headers（包含用户信息）
        2. 转发请求
        3. 处理响应
        """
        headers = self._build_langgraph_headers(user_info, request.headers)
        
        response = await self.http_client.request(
            method=request.method,
            url=f"{self.langgraph_url}{request.path}",
            headers=headers,
            json=await request.json() if request.method in ["POST", "PUT"] else None,
        )
        
        return response
重要：确保所有适配器方法都使用这个统一的转发逻辑

Phase 5: 简化接口暴露
目标：对外只暴露必要的接口，隐藏实现细节
接口设计：
外部 API（应用端调用）           内部转换为 LangGraph API
─────────────────────────────────────────────────────────

POST   /conversations           →  POST /threads
GET    /conversations           →  POST /threads/search (带 filter)
GET    /conversations/{id}      →  GET  /threads/{id}
DELETE /conversations/{id}      →  DELETE /threads/{id}

POST   /conversations/{id}/messages  →  POST /threads/{id}/runs
GET    /conversations/{id}/messages  →  GET  /threads/{id}/history
GET    /conversations/{id}/stream    →  SSE /threads/{id}/runs/stream
好处：

应用端不需要知道 LangGraph 的实现细节
可以自由更换底层 Agent 框架
接口命名更符合业务语义


Phase 6: 实现会话管理（游客支持）
目标：为游客提供会话管理能力
逻辑说明：
python# 伪代码

class GuestSessionManager:
    """
    游客会话管理
    
    游客没有用户账号，通过 session 识别身份
    """
    
    def __init__(self, redis_client):
        self.redis = redis_client
        self.session_ttl = 7 * 24 * 3600  # 7 天过期
    
    async def create_session(self) -> str:
        """
        创建游客会话
        
        1. 生成唯一 session_id
        2. 存储到 Redis，设置过期时间
        3. 返回 session_id
        """
        session_id = f"guest_{uuid4().hex}"
        await self.redis.setex(
            f"guest_session:{session_id}",
            self.session_ttl,
            json.dumps({"created_at": time.time()})
        )
        return session_id
    
    async def validate_session(self, session_id: str) -> bool:
        """验证会话是否有效"""
        return await self.redis.exists(f"guest_session:{session_id}")
    
    async def refresh_session(self, session_id: str):
        """刷新会话过期时间"""
        await self.redis.expire(f"guest_session:{session_id}", self.session_ttl)
    
    async def convert_to_user(self, session_id: str, user_id: str):
        """
        游客转正式用户
        
        1. 获取游客的所有 Thread
        2. 更新 Thread 的 owner metadata
        3. 删除游客 session
        """

Phase 7: 添加请求日志和监控
目标：记录所有请求用于审计和调试
逻辑说明：
python# 伪代码

class RequestLoggingMiddleware:
    """
    请求日志中间件
    
    记录：
    1. 请求基本信息（时间、方法、路径）
    2. 用户信息（脱敏）
    3. 响应状态和耗时
    4. 异常信息（如果有）
    """
    
    async def __call__(self, request, call_next):
        start_time = time.time()
        request_id = str(uuid4())
        
        # 记录请求
        log_data = {
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
            "method": request.method,
            "path": request.path,
            "user_id": request.state.user.get("user_id", "anonymous"),
            "user_type": request.state.user.get("user_type", "unknown"),
            "client_ip": self.get_client_ip(request),
        }
        
        try:
            response = await call_next(request)
            log_data["status"] = response.status_code
            log_data["duration_ms"] = (time.time() - start_time) * 1000
            
            # 异步写入日志（不阻塞响应）
            asyncio.create_task(self.write_log(log_data))
            
            return response
            
        except Exception as e:
            log_data["status"] = 500
            log_data["error"] = str(e)
            log_data["duration_ms"] = (time.time() - start_time) * 1000
            asyncio.create_task(self.write_log(log_data))
            raise

Phase 8: 编写集成测试
目标：端到端测试整个链路
测试场景：
python# 伪代码测试用例

class TestEndToEnd:
    
    # === 鉴权测试 ===
    
    async def test_valid_token_accepted(self):
        """有效 token 可以访问"""
        token = create_test_token(user_id="user_123")
        response = await client.post(
            "/conversations",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
    
    async def test_invalid_token_rejected(self):
        """无效 token 被拒绝"""
        response = await client.post(
            "/conversations",
            headers={"Authorization": "Bearer invalid_token"}
        )
        assert response.status_code == 401
    
    async def test_guest_session_works(self):
        """游客会话可以访问"""
        # 创建游客会话
        session = await client.post("/guest/session")
        session_id = session.json()["session_id"]
        
        # 使用游客会话创建对话
        response = await client.post(
            "/conversations",
            headers={"X-Guest-Session": session_id}
        )
        assert response.status_code == 200
    
    # === 限流测试 ===
    
    async def test_user_rate_limit(self):
        """用户级限流生效"""
        token = create_test_token(user_id="user_123")
        
        # 快速发送超过限制的请求
        for i in range(35):
            response = await client.post(
                "/conversations",
                headers={"Authorization": f"Bearer {token}"}
            )
            if i >= 30:  # 超过 30 req/min 限制
                assert response.status_code == 429
    
    # === 用户隔离测试 ===
    
    async def test_user_isolation(self):
        """用户之间数据隔离"""
        # User A 创建对话
        token_a = create_test_token(user_id="user_a")
        conv_a = await client.post(
            "/conversations",
            headers={"Authorization": f"Bearer {token_a}"}
        )
        conv_id = conv_a.json()["conversation_id"]
        
        # User B 尝试访问
        token_b = create_test_token(user_id="user_b")
        response = await client.get(
            f"/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {token_b}"}
        )
        assert response.status_code in [403, 404]
    
    # === 流式输出测试 ===
    
    async def test_streaming_works(self):
        """流式输出正常工作"""
        token = create_test_token(user_id="user_123")
        
        # 创建对话
        conv = await client.post(
            "/conversations",
            headers={"Authorization": f"Bearer {token}"}
        )
        conv_id = conv.json()["conversation_id"]
        
        # 发送消息并接收流式响应
        async with client.stream(
            "POST",
            f"/conversations/{conv_id}/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "Hello"}
        ) as response:
            events = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
            
            assert len(events) > 0
            assert any(e.get("type") == "message" for e in events)

验收标准
完成以上 Phase 后，需要满足：

✅ 有效 token 可以正常访问所有接口
✅ 无效/过期 token 返回 401
✅ 游客可以通过 session 访问（受限功能）
✅ 限流在各维度正常生效
✅ 用户 A 无法访问用户 B 的数据
✅ 流式输出正常工作
✅ 请求日志正确记录
✅ 所有测试用例通过