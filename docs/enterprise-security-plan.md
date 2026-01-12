# Agent Gateway 企业级权限管理系统优化计划

> 最后更新: 2026-01-12
> 版本: 2.1
> 状态: 部分实现（模块5已完成，其他待实现）

## 概述

基于 Codex 安全审计结果和2025年业界最佳实践，本计划旨在解决 Agent Gateway 中的权限管理漏洞，使其成为完整的企业级权限管理系统。

### 业务背景
- 内部公司系统（单租户模式）
- Assistants/Services 默认私有（仅创建者可访问）
- Confluence 访问权限：仅创建者和管理员
- 所有模块并行修复

---

# 第一部分：安全审计问题修复

## 问题清单

| 严重级别 | 模块 | 问题描述 | 状态 |
|---------|------|---------|------|
| CRITICAL | LangGraph Tools | KB检索工具默认使用admin/system上下文，绕过数据集ACL | 待修复 |
| CRITICAL | LangGraph Proxy | Assistant缓存仅按assistant_id键控，导致跨用户数据泄露 | 待修复 |
| CRITICAL | LangGraph Proxy | get_assistant()无所有权验证，create_run()不验证assistant访问权 | 待修复 |
| HIGH | Confluence | get_binding无租户验证，可跨租户访问绑定/页面/任务 | 待修复 |
| HIGH | Services | 服务调用仅由全局service:invoke控制，无owner/tenant隔离 | 已修复 |
| MEDIUM | Auth Context | UserContext忽略DB角色/权限，Knowledge/Confluence使用UserContext | 已修复 |

---

## 模块1: Agent/Assistant 隔离修复

**关键文件:**
- `src/adapters/langgraph_proxy.py`
- `src/api/v1/langgraph.py`
- `src/api/v1/conversations.py`

**步骤:**

1. **添加异常类** (langgraph_proxy.py:93后)
   ```python
   class AssistantNotFoundError(LangGraphProxyError): ...
   class AssistantAccessDeniedError(LangGraphProxyError): ...
   ```

2. **添加所有权验证方法** (langgraph_proxy.py:1176后)
   ```python
   def _verify_assistant_ownership(self, assistant, user, require_write=False):
       # 访问规则:
       # 1. admin用户可访问所有
       # 2. 创建者(created_by)可完全访问
       # 3. shared_with列表中的用户可读取
       # 4. is_public=True的assistant所有人可读取
   ```

3. **修复Assistant缓存键** (langgraph_proxy.py:358-396)
   - 将缓存键从 `assistant_id` 改为 `f"{assistant_id}:{user.user_id}"`
   - 在获取后添加所有权验证

4. **添加Run操作验证**
   - `create_run()` (line 689): 在thread验证后添加assistant验证
   - `create_run_wait()` (line 754): 添加assistant验证
   - `stream_run()` (line 818): 添加assistant验证
   - `create_stateless_run()` (line 1010): 添加assistant验证（关键！）

5. **更新API层异常处理** (langgraph.py, conversations.py)
   - 添加 AssistantNotFoundError → 404
   - 添加 AssistantAccessDeniedError → 403

6. **（可选）添加共享API**
   - POST /assistants/{id}/share
   - DELETE /assistants/{id}/share

---

## 模块2: 知识库ACL强制执行

**关键文件:**
- `src/services/knowledge/langgraph_tools.py`

**步骤:**

1. **移除系统上下文默认值** (line 107-122)
   - 删除或重命名 `_create_system_context()` 方法
   - 如需保留用于后台任务，添加警告注释

2. **修改KnowledgeRetriever构造函数** (line 83-111)
   ```python
   def __init__(self, ..., user_context: UserContext):  # 移除Optional和默认值
       if user_context is None:
           raise AuthenticationRequiredError("User context required")
       if not user_context.is_authenticated:
           raise AuthenticationRequiredError("Authenticated user required")
   ```

3. **修改MultiDatasetRetriever构造函数** (line 306-330)
   - 同样移除Optional，要求必传user_context
   - 验证用户已认证

4. **修改DifyCompatibleKBAPI构造函数** (line 480-486)
   - 移除default_user_context默认值
   - 要求必传且已认证的user_context

5. **更新create_retrieval_tool工厂函数** (line 417-455)
   - 移除Optional，要求必传user_context

---

## 模块3: Confluence ACL修复

**关键文件:**
- `src/services/knowledge/confluence/sync_service.py`
- `src/api/v1/confluence.py`
- `src/persistence/database.py`

**步骤:**

1. **数据库迁移** - 添加owner_id字段 ✅ 已在 011_confluence_acl.sql 实现
   ```sql
   -- 三个表都需要添加 owner_id
   ALTER TABLE confluence_connections ADD COLUMN owner_id VARCHAR(255);
   ALTER TABLE confluence_space_bindings ADD COLUMN owner_id VARCHAR(255);
   ALTER TABLE confluence_sync_tasks ADD COLUMN owner_id VARCHAR(255);

   -- 回填数据
   UPDATE confluence_connections SET owner_id = created_by WHERE owner_id IS NULL;
   UPDATE confluence_space_bindings SET owner_id = created_by WHERE owner_id IS NULL;
   -- sync_tasks 从关联的 binding 获取 owner_id
   UPDATE confluence_sync_tasks t SET owner_id = b.owner_id
   FROM confluence_space_bindings b WHERE t.binding_id = b.binding_id AND t.owner_id IS NULL;
   ```

2. **添加权限验证辅助方法** (sync_service.py)
   ```python
   async def _verify_confluence_access(self, resource_type, resource_id, user, required="viewer"):
       # 访问规则:
       # 1. Admin用户完全访问
       # 2. Owner (owner_id或created_by匹配) 完全访问
       # 3. 其他用户拒绝访问
   ```

3. **修改服务方法签名** - 添加user参数:
   - `get_binding(binding_id, user)` - line 1212
   - `get_connection(connection_id, user)` - line 342
   - `list_connections(user, status)` - line 334
   - `list_bindings(user, connection_id, dataset_id)` - line 1160
   - `get_sync_status(binding_id, user)` - line 1573
   - `list_pages(binding_id, user, ...)` - line 1937
   - `list_sync_tasks(user, binding_id, ...)` - line 2045
   - `get_sync_task(task_id, user)` - line 2068

4. **更新API路由** (confluence.py) - 传递user上下文:
   - GET /bindings/{binding_id} (line 432)
   - GET /bindings/{binding_id}/status (line 658)
   - GET /bindings/{binding_id}/pages (line 674)
   - GET /tasks (line 764)
   - GET /tasks/{task_id} (line 786)
   - GET /connections (line 101)
   - GET /bindings (line 402)

5. **添加数据库方法**
   - `list_confluence_connections_by_owner(owner_id, status)`

---

## 模块4: 服务隔离增强（低优先级）

**关键文件:**
- `src/core/request/validator.py`
- `src/core/service/service.py`

**建议步骤:**

1. 在ServiceDefinition中添加owner_id字段
2. 在validator中添加服务所有权验证
3. 考虑添加细粒度权限如 `service:invoke:{service_id}`

---

## 模块5: 上下文统一 ✅ 已完成

**关键文件:**
- `src/api/deps.py`

**已实现:**

1. 在UserContext获取时合并DB角色/权限
2. JWT和API Key路径都统一合并DB权限

---

# 第二部分：企业级最佳实践完善计划

## 基于业界标准的差距分析

根据2025年企业级AI平台安全最佳实践，以下是当前系统的差距：

### 参考资料
- [Obsidian Security - AI Agent Security 2025](https://www.obsidiansecurity.com/blog/security-for-ai-agents)
- [LangChain Auth Docs](https://docs.langchain.com/langgraph-platform/auth)
- [SOC2 AI Compliance](https://quantarra.io/blog/soc-2-ai-compliance-news-2025-edition-the-trends-that-reshaped-security-audits)

### 差距分析表

| 领域 | 业界标准 | 当前状态 | 差距级别 |
|-----|---------|---------|---------|
| **审计日志** | 不可变、加密、签名的审计日志 | 基础数据库存储，可修改 | HIGH |
| **权限拒绝记录** | 所有授权失败必须记录 | 仅记录登录失败 | HIGH |
| **Token轮换** | 自动轮换（24-72小时） | 无自动轮换机制 | MEDIUM |
| **刷新Token** | 短期访问Token+长期刷新Token | 仅3小时固定Token | MEDIUM |
| **密钥管理** | HashiCorp Vault/AWS Secrets Manager | 环境变量存储 | HIGH |
| **ABAC/PBAC** | 基于属性/策略的动态授权 | 静态RBAC | MEDIUM |
| **限流通信** | 标准化限流头(X-RateLimit-*) | 部分实现 | LOW |
| **合规报告** | 自动SOC2/GDPR报告导出 | 无 | MEDIUM |
| **MFA/2FA** | 多因素认证 | 无 | HIGH |
| **会话管理** | 并发会话限制、强制登出 | 无 | MEDIUM |

---

## 模块6: 企业级审计日志系统 (HIGH)

**关键文件:**
- src/persistence/database.py
- src/core/middleware/audit.py (新建)
- src/api/v1/admin/audit.py (新建)
- database/migrations/012_audit_enhancement.sql (新建)

### 步骤:

1. **增强审计日志表结构**
   ```sql
   -- 添加防篡改字段
   ALTER TABLE audit_logs ADD COLUMN previous_hash VARCHAR(64);
   ALTER TABLE audit_logs ADD COLUMN current_hash VARCHAR(64);
   ALTER TABLE audit_logs ADD COLUMN signature TEXT;

   -- 设置为追加专用（可选）
   CREATE OR REPLACE FUNCTION prevent_audit_update()
   RETURNS TRIGGER AS $$
   BEGIN
       IF TG_OP = 'UPDATE' OR TG_OP = 'DELETE' THEN
           RAISE EXCEPTION 'Audit logs are immutable';
       END IF;
       RETURN NEW;
   END;
   $$ LANGUAGE plpgsql;

   CREATE TRIGGER audit_immutable_trigger
   BEFORE UPDATE OR DELETE ON audit_logs
   FOR EACH ROW EXECUTE FUNCTION prevent_audit_update();
   ```

2. **添加审计中间件** (src/core/middleware/audit.py)
   ```python
   class AuditMiddleware:
       async def __call__(self, request, call_next):
           # 记录所有API请求
           # 特别记录：授权失败、敏感操作、数据访问

       async def log_permission_denied(self, user, resource, action, reason):
           # 权限拒绝专用记录

       async def log_sensitive_operation(self, user, operation, details):
           # 敏感操作记录（删除、配置变更等）
   ```

3. **添加审计日志API** (src/api/v1/admin/audit.py)
   ```python
   @router.get("/audit-logs")
   async def list_audit_logs(
       event_type: Optional[str],
       user_id: Optional[str],
       start_time: datetime,
       end_time: datetime,
       auth: AuthContext = Depends(get_auth_context),
   ):
       # 仅管理员可访问
       # 支持导出CSV/JSON

   @router.get("/audit-logs/export")
   async def export_audit_logs(format: str = "csv"):
       # SOC2合规报告导出
   ```

4. **添加链式哈希验证**
   ```python
   def calculate_audit_hash(log_entry, previous_hash):
       data = f"{previous_hash}:{log_entry.event_type}:{log_entry.user_id}:{log_entry.created_at}"
       return hashlib.sha256(data.encode()).hexdigest()

   def verify_audit_chain(logs):
       # 验证审计日志链完整性
   ```

---

## 模块7: Token管理增强 (MEDIUM)

**关键文件:**
- src/core/auth/token_manager.py (新建)
- src/api/v1/auth.py
- src/persistence/database.py

### 步骤:

1. **实现刷新Token机制**
   ```python
   class TokenManager:
       ACCESS_TOKEN_TTL = timedelta(minutes=15)  # 短期
       REFRESH_TOKEN_TTL = timedelta(days=7)     # 长期

       async def create_token_pair(self, user_id) -> TokenPair:
           access_token = self._create_access_token(user_id)
           refresh_token = self._create_refresh_token(user_id)
           return TokenPair(access=access_token, refresh=refresh_token)

       async def refresh_access_token(self, refresh_token) -> str:
           # 验证refresh token
           # 生成新access token
           # 可选：轮换refresh token
   ```

2. **添加Token轮换策略**
   ```python
   class TokenRotationPolicy:
       def should_rotate(self, token_age: timedelta) -> bool:
           # 超过阈值时建议轮换

       async def rotate_api_key(self, old_key_id: str) -> ApiKey:
           # 生成新密钥，设置旧密钥宽限期
   ```

3. **添加API端点**
   ```python
   @router.post("/auth/refresh")
   async def refresh_token(refresh_token: str):
       # 刷新access token

   @router.post("/api-keys/{key_id}/rotate")
   async def rotate_api_key(key_id: str, grace_period_hours: int = 24):
       # API Key轮换，保留旧密钥24小时
   ```

---

## 模块8: 会话管理增强 (MEDIUM)

**关键文件:**
- src/core/auth/session_manager.py (新建)
- src/persistence/database.py
- database/migrations/013_session_management.sql (新建)

### 步骤:

1. **添加会话表**
   ```sql
   CREATE TABLE user_sessions (
       id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
       user_id VARCHAR(255) NOT NULL,
       session_token VARCHAR(255) NOT NULL UNIQUE,
       device_info JSONB,
       ip_address VARCHAR(45),
       last_activity TIMESTAMPTZ DEFAULT NOW(),
       expires_at TIMESTAMPTZ NOT NULL,
       is_active BOOLEAN DEFAULT true,
       created_at TIMESTAMPTZ DEFAULT NOW()
   );

   CREATE INDEX idx_user_sessions_user_id ON user_sessions(user_id);
   CREATE INDEX idx_user_sessions_active ON user_sessions(user_id, is_active);
   ```

2. **实现会话管理器**
   ```python
   class SessionManager:
       MAX_CONCURRENT_SESSIONS = 5  # 可配置

       async def create_session(self, user_id, device_info, ip) -> Session:
           # 检查并发会话数
           active = await self.count_active_sessions(user_id)
           if active >= self.MAX_CONCURRENT_SESSIONS:
               await self.revoke_oldest_session(user_id)
           return await self._create_new_session(...)

       async def revoke_all_sessions(self, user_id):
           # 强制登出所有设备

       async def list_active_sessions(self, user_id) -> List[Session]:
           # 列出用户所有活跃会话
   ```

3. **添加API端点**
   ```python
   @router.get("/auth/sessions")
   async def list_my_sessions(user: UserContext):
       # 查看我的所有登录会话

   @router.delete("/auth/sessions/{session_id}")
   async def revoke_session(session_id: str, user: UserContext):
       # 登出指定设备

   @router.delete("/auth/sessions")
   async def revoke_all_sessions(user: UserContext):
       # 登出所有设备
   ```

---

## 模块9: 细粒度限流增强 (LOW)

**关键文件:**
- src/core/gateway/multi_dimension_rate_limiter.py
- src/api/deps.py

### 步骤:

1. **标准化限流响应头**
   ```python
   class RateLimitHeaders:
       @staticmethod
       def build(result: RateLimitResult) -> Dict[str, str]:
           return {
               "X-RateLimit-Limit": str(result.limit),
               "X-RateLimit-Remaining": str(result.remaining),
               "X-RateLimit-Reset": str(int(result.reset_at.timestamp())),
               "X-RateLimit-Policy": result.policy_name,
               "Retry-After": str(result.retry_after) if not result.allowed else None,
           }
   ```

2. **添加配额管理（长期限制）**
   ```python
   class QuotaManager:
       async def check_monthly_quota(self, user_id, resource) -> QuotaResult:
           # 检查月度配额（token使用量、API调用次数等）

       async def get_quota_usage(self, user_id) -> QuotaUsage:
           # 获取当前配额使用情况
   ```

3. **添加配额查询端点**
   ```python
   @router.get("/usage/quota")
   async def get_my_quota(user: UserContext):
       # 返回当前用户配额使用情况
       return {
           "api_calls": {"used": 1000, "limit": 10000, "reset_at": "2025-02-01"},
           "tokens": {"used": 50000, "limit": 1000000, "reset_at": "2025-02-01"},
       }
   ```

---

## 模块10: 密钥管理集成 (HIGH - 可选)

**说明:** 如果未来需要更高安全级别，可考虑集成外部密钥管理服务。

### 选项A: HashiCorp Vault集成
```python
class VaultSecretProvider:
    def __init__(self, vault_addr, vault_token):
        self.client = hvac.Client(url=vault_addr, token=vault_token)

    async def get_secret(self, path: str) -> str:
        return self.client.secrets.kv.v2.read_secret_version(path)

    async def rotate_secret(self, path: str) -> str:
        # 自动轮换密钥
```

### 选项B: AWS Secrets Manager集成
```python
class AWSSecretProvider:
    async def get_secret(self, secret_id: str) -> str:
        client = boto3.client('secretsmanager')
        return client.get_secret_value(SecretId=secret_id)
```

---

## 模块11: MFA/2FA支持 (HIGH - 可选)

**关键文件:**
- src/core/auth/mfa.py (新建)
- src/api/v1/auth.py

### 步骤:

1. **TOTP实现**
   ```python
   import pyotp

   class TOTPManager:
       def generate_secret(self) -> str:
           return pyotp.random_base32()

       def verify_code(self, secret: str, code: str) -> bool:
           totp = pyotp.TOTP(secret)
           return totp.verify(code)

       def get_provisioning_uri(self, secret: str, email: str) -> str:
           return pyotp.totp.TOTP(secret).provisioning_uri(
               name=email, issuer_name="Agent Gateway"
           )
   ```

2. **数据库字段**
   ```sql
   ALTER TABLE users ADD COLUMN mfa_enabled BOOLEAN DEFAULT false;
   ALTER TABLE users ADD COLUMN mfa_secret VARCHAR(32);
   ALTER TABLE users ADD COLUMN mfa_backup_codes TEXT[];
   ```

3. **API端点**
   ```python
   @router.post("/auth/mfa/setup")
   async def setup_mfa(user: UserContext):
       # 返回QR code和备用码

   @router.post("/auth/mfa/verify")
   async def verify_mfa(code: str, user: UserContext):
       # 验证并启用MFA

   @router.post("/auth/login")
   async def login_with_mfa(credentials, mfa_code: Optional[str]):
       # 登录时验证MFA
   ```

---

# 第三部分：实现路线图

## 实现优先级

### 第一优先级 (企业必备)
| 模块 | 优先级 | 预估工时 | 影响 |
|-----|-------|---------|------|
| 模块1: Assistant隔离 | CRITICAL | 1-2天 | 数据安全 |
| 模块2: KB ACL | CRITICAL | 1天 | 数据安全 |
| 模块3: Confluence ACL | HIGH | 1天 | 数据安全 |
| 模块6: 审计日志增强 | HIGH | 2-3天 | SOC2合规基础 |

### 第二优先级 (推荐)
| 模块 | 优先级 | 预估工时 | 影响 |
|-----|-------|---------|------|
| 模块7: Token管理增强 | MEDIUM | 1-2天 | 安全性提升 |
| 模块8: 会话管理 | MEDIUM | 1-2天 | 安全管理 |
| 模块9: 限流增强 | LOW | 0.5天 | 用户体验 |

### 第三优先级 (可选)
| 模块 | 优先级 | 预估工时 | 影响 |
|-----|-------|---------|------|
| 模块4: 服务隔离 | LOW | 1天 | 安全增强 |
| 模块10: Vault集成 | HIGH | 2天 | 密钥安全 |
| 模块11: MFA支持 | HIGH | 2天 | 账户安全 |

---

## 实现阶段

### 阶段一：安全基础 (模块1-3)
**目标**: 修复所有CRITICAL和HIGH级别安全问题

1. Assistant所有权验证和缓存隔离
2. KB ACL强制执行
3. Confluence访问控制

### 阶段二：合规基础 (模块6)
**目标**: SOC2合规审计日志系统

1. 数据库迁移 - 添加防篡改字段和触发器
2. 实现AuditMiddleware - 记录所有权限拒绝
3. 实现链式哈希验证 - 防篡改检测
4. 添加审计日志API - 查询和导出功能

### 阶段三：安全增强 (模块7 + 模块8)
**目标**: Token管理和会话控制

1. 实现TokenManager - 刷新Token机制
2. 实现API Key轮换 - 带宽限期
3. 实现SessionManager - 并发会话限制
4. 添加会话管理API - 设备管理

### 阶段四：用户体验 (模块9)
**目标**: 限流增强

1. 标准化限流响应头
2. 实现配额管理
3. 添加配额查询API

### 阶段五：高级安全 (模块10 + 模块11)
**目标**: 密钥管理和MFA

1. (可选) Vault/AWS Secrets Manager集成
2. 实现TOTP MFA
3. 添加MFA API端点

---

## 关键新增文件清单

| 文件 | 说明 |
|-----|------|
| database/migrations/011_confluence_acl.sql | Confluence ACL ✅ 已存在 |
| database/migrations/012_audit_enhancement.sql | 审计日志增强 |
| database/migrations/013_session_management.sql | 会话管理表 |
| database/migrations/014_mfa_support.sql | MFA字段 |
| src/core/middleware/audit.py | 审计中间件 |
| src/core/auth/token_manager.py | Token管理器 |
| src/core/auth/session_manager.py | 会话管理器 |
| src/core/auth/mfa.py | MFA支持 |
| src/api/v1/admin/audit.py | 审计日志API |

---

## 合规检查清单

### SOC2 Trust Service Criteria
- [ ] **CC6.1**: 逻辑访问控制 → 已有RBAC，需增强审计
- [ ] **CC6.2**: 授权变更记录 → 需要审计日志增强
- [ ] **CC6.3**: 用户访问评审 → 需要审计日志API
- [ ] **CC7.2**: 安全事件监控 → 需要权限拒绝记录
- [ ] **CC7.3**: 事件响应 → 需要审计日志导出

### GDPR 相关
- [ ] **数据访问记录**: 谁访问了什么数据 → 审计日志
- [ ] **数据主体权利**: 导出/删除用户数据 → 需要实现
- [ ] **同意管理**: 数据处理同意记录 → 可选

---

## 验证清单

### 1. Assistant隔离验证
```bash
# 用户A创建assistant
# 用户B尝试访问 → 应返回403
# 用户B尝试create_run → 应返回403
```

### 2. KB ACL验证
```bash
# 创建私有数据集
# 不传user_context调用retriever → 应抛出异常
# 传guest用户 → 应抛出异常
# 传有权限用户 → 应成功
```

### 3. Confluence ACL验证
```bash
# 用户A创建binding
# 用户B GET /bindings/{id} → 应返回403
# Admin GET /bindings/{id} → 应成功
```

### 4. 审计日志验证
```bash
# 触发权限拒绝
curl -H "Authorization: Bearer invalid" /api/v1/config/auth
# 验证是否记录到audit_logs

# 验证日志不可篡改
UPDATE audit_logs SET user_id = 'hacker' WHERE id = 1;
# 应该被拒绝
```

### 5. Token轮换验证
```bash
# 创建API Key
POST /api/v1/admin/api-keys
# 轮换
POST /api/v1/admin/api-keys/{id}/rotate?grace_period_hours=24
# 验证旧密钥在宽限期内仍可用
# 验证宽限期后旧密钥失效
```

### 6. 会话管理验证
```bash
# 登录多个设备
# 验证达到上限时旧会话被踢出
# 验证强制登出所有设备功能
```

### 7. 运行测试套件
```bash
pytest tests/adapters/test_langgraph*.py -v
pytest tests/services/test_knowledge*.py -v
pytest tests/api/v1/test_confluence*.py -v
pytest tests/ -v --tb=short
```

---

## 风险评估

| 风险 | 缓解措施 |
|-----|---------|
| 破坏现有assistant访问 | 为系统/默认assistant添加is_public: true |
| 缓存失效复杂性 | 使用模式匹配的Redis键删除 |
| 向后兼容性 | 共享元数据可选，默认仅创建者访问 |
| 性能影响 | 利用现有缓存基础设施 |

---

## 参考资料

- [Obsidian Security - AI Agent Security 2025](https://www.obsidiansecurity.com/blog/security-for-ai-agents)
- [LangChain Authentication & Access Control](https://docs.langchain.com/langgraph-platform/auth)
- [SOC2 AI Compliance 2025](https://quantarra.io/blog/soc-2-ai-compliance-news-2025-edition-the-trends-that-reshaped-security-audits)
- [Enterprise API Rate Limiting Best Practices](https://zuplo.com/blog/2025/01/06/10-best-practices-for-api-rate-limiting-in-2025)
- [AI Agent RBAC Framework](https://medium.com/@christopher_79834/ai-agent-rbac-essential-security-framework-for-enterprise-ai-deployment-d9d1d4711183)
- [Cerbos RAG Authorization](https://www.cerbos.dev/blog/rag-authorization-system-langgraph-cerbos-pinecone)
