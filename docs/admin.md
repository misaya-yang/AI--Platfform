# AI Gateway 账户管理和权限管理模块 - 实现计划

> **文档版本**: 1.0.0
> **创建日期**: 2026-01-05
> **状态**: 待审核

---

## 1. 项目概述

### 1.1 目标
为AI服务网关平台实现完整的账户管理和权限管理功能，实现管理控制台与对话功能的权限解耦，支持精细化的权限分配。

### 1.2 核心需求

| 功能模块 | 需求描述 |
|---------|---------|
| **账户管理** | 邮箱登录、初始密码、强制改密、用户启用/禁用 |
| **权限管理** | 基础权限划分、角色分配、按功能模块控制 |
| **审计日志** | 登录/登出记录、密码变更记录 |
| **数据库迁移** | 完整的迁移脚本，支持增量更新 |

### 1.3 用户约束

| 约束项 | 要求 |
|-------|------|
| 邮箱域名 | 仅允许 `@hejazfs.com.au` |
| 初始密码 | `111111` |
| 密码策略 | 最少8位 + 字母 + 数字 + 特殊字符 |
| 强制改密 | 首次登录必须修改密码 |
| Admin账号 | `admin` / `123456.dc` |
| 目标用户 | CS、Sales团队，未来可扩展 |

---

## 2. 数据库设计

### 2.1 迁移脚本
**文件**: `database/migrations/005_account_permission_system.sql`

### 2.2 表结构变更

#### 2.2.1 users 表增强

```sql
-- 新增字段
ALTER TABLE users ADD COLUMN password_hash VARCHAR(255);
ALTER TABLE users ADD COLUMN force_password_change BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN password_changed_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN last_login_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN last_login_ip VARCHAR(45);
ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN created_by VARCHAR(255);
```

#### 2.2.2 permissions 表（新建）

```sql
CREATE TABLE permissions (
    id SERIAL PRIMARY KEY,
    permission_code VARCHAR(100) NOT NULL UNIQUE,  -- 如: console:dashboard:view
    name VARCHAR(255) NOT NULL,                    -- 显示名称
    description TEXT,                              -- 描述
    category VARCHAR(50) NOT NULL,                 -- 分类: console/conversation/knowledge/user_management
    resource VARCHAR(100) NOT NULL,                -- 资源: dashboard/services/playground
    action VARCHAR(50) NOT NULL,                   -- 动作: view/edit/create/delete
    is_system BOOLEAN NOT NULL DEFAULT FALSE,      -- 是否系统内置
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

#### 2.2.3 role_permissions 表（新建）

```sql
CREATE TABLE role_permissions (
    id SERIAL PRIMARY KEY,
    role_name VARCHAR(100) NOT NULL REFERENCES rbac_roles(role_name) ON DELETE CASCADE,
    permission_code VARCHAR(100) NOT NULL REFERENCES permissions(permission_code) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(role_name, permission_code)
);
```

#### 2.2.4 user_roles 表（新建）

```sql
CREATE TABLE user_roles (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role_name VARCHAR(100) NOT NULL REFERENCES rbac_roles(role_name) ON DELETE CASCADE,
    granted_by VARCHAR(255),                      -- 授权人
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                       -- 过期时间（可选）
    UNIQUE(user_id, role_name)
);
```

#### 2.2.5 login_audit 表（新建）

```sql
CREATE TABLE login_audit (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255),
    email VARCHAR(255),
    action VARCHAR(50) NOT NULL,    -- login_success/login_failed/logout/password_change
    ip_address VARCHAR(45),
    user_agent TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 2.3 预置权限列表

| 分类 | 权限代码 | 名称 | 描述 |
|-----|---------|------|------|
| **控制台** | console:dashboard:view | 查看仪表盘 | 访问仪表盘概览 |
| | console:settings:view | 查看设置 | 访问系统设置页面 |
| | console:settings:edit | 编辑设置 | 修改系统设置 |
| | console:services:view | 查看服务 | 访问服务管理 |
| | console:services:edit | 编辑服务 | 修改服务配置 |
| **对话** | conversation:playground:access | 访问Playground | 使用AI对话功能 |
| | conversation:thread:create | 创建会话 | 创建新对话线程 |
| | conversation:thread:delete | 删除会话 | 删除对话线程 |
| **知识库** | knowledge:dataset:view | 查看数据集 | 访问知识库数据集 |
| | knowledge:dataset:create | 创建数据集 | 创建新数据集 |
| | knowledge:dataset:edit | 编辑数据集 | 修改数据集配置 |
| | knowledge:dataset:delete | 删除数据集 | 删除数据集 |
| | knowledge:document:upload | 上传文档 | 上传文档到数据集 |
| | knowledge:confluence:manage | 管理Confluence | 管理Confluence集成 |
| **用户管理** | user:list | 用户列表 | 查看用户列表 |
| | user:create | 创建用户 | 创建新用户 |
| | user:edit | 编辑用户 | 修改用户信息 |
| | user:delete | 删除用户 | 删除用户 |
| | user:role:assign | 分配角色 | 为用户分配角色 |
| | role:list | 角色列表 | 查看角色列表 |
| | role:create | 创建角色 | 创建新角色 |
| | role:edit | 编辑角色 | 修改角色权限 |
| | role:delete | 删除角色 | 删除角色 |
| **管理员** | admin:* | 完全访问 | 管理员全权限（通配符） |

### 2.4 预置角色

| 角色 | 描述 | 权限 |
|-----|------|------|
| **admin** | 系统管理员 | `admin:*` |
| **manager** | 部门经理 | 控制台查看、对话功能、知识库创建、用户列表 |
| **cs_staff** | 客服人员 | 仪表盘查看、对话功能、知识库查看 |
| **sales_staff** | 销售人员 | 仪表盘查看、对话功能、知识库查看 |

### 2.5 初始Admin账号

```sql
INSERT INTO users (
    user_id, username, email, display_name, tenant_id, tier,
    roles, status, password_hash, force_password_change
) VALUES (
    'admin', 'admin', 'admin@hejazfs.com.au', 'System Administrator',
    'default', 'admin', ARRAY['admin'], 'active',
    '$2b$12$...',  -- bcrypt hash of '123456.dc'
    FALSE          -- admin不需要强制改密
);
```

---

## 3. 后端API设计

### 3.1 新增文件

#### 3.1.1 src/core/auth/password.py

密码工具模块：
- `hash_password(password: str) -> str` - bcrypt哈希
- `verify_password(password: str, hashed: str) -> bool` - 验证密码
- `validate_password_strength(password: str) -> List[str]` - 密码强度校验
- `is_valid_email_domain(email: str) -> bool` - 邮箱域名校验

```python
ALLOWED_EMAIL_DOMAIN = "hejazfs.com.au"
DEFAULT_PASSWORD = "111111"
MIN_PASSWORD_LENGTH = 8
```

#### 3.1.2 src/api/v1/auth.py

认证API路由：

| 端点 | 方法 | 描述 | 权限 |
|-----|------|------|------|
| `/api/v1/auth/login` | POST | 用户登录 | 公开 |
| `/api/v1/auth/logout` | POST | 用户登出 | 需认证 |
| `/api/v1/auth/change-password` | POST | 修改密码 | 需认证 |
| `/api/v1/auth/me` | GET | 获取当前用户信息 | 需认证 |

**登录流程**:
1. 验证邮箱域名 (@hejazfs.com.au)
2. 检查账户锁定状态
3. 验证密码
4. 登录失败计数（5次锁定30分钟）
5. 生成JWT Token
6. 返回用户信息和force_password_change标记

#### 3.1.3 src/api/v1/users.py

用户管理API：

| 端点 | 方法 | 描述 | 权限 |
|-----|------|------|------|
| `/api/v1/users` | GET | 用户列表 | user:list |
| `/api/v1/users` | POST | 创建用户 | user:create |
| `/api/v1/users/{user_id}` | GET | 获取用户 | user:list |
| `/api/v1/users/{user_id}` | PUT | 更新用户 | user:edit |
| `/api/v1/users/{user_id}` | DELETE | 删除用户 | user:delete |
| `/api/v1/users/{user_id}/reset-password` | POST | 重置密码 | user:edit |

**创建用户流程**:
1. 验证邮箱域名
2. 检查邮箱是否已存在
3. 从邮箱生成user_id（如 misaya.yang -> misaya_yang）
4. 使用默认密码111111创建账户
5. 设置force_password_change = true
6. 分配指定角色

#### 3.1.4 src/api/v1/roles.py

角色权限API：

| 端点 | 方法 | 描述 | 权限 |
|-----|------|------|------|
| `/api/v1/roles` | GET | 角色列表 | role:list |
| `/api/v1/roles` | POST | 创建角色 | role:create |
| `/api/v1/roles/{role_name}` | PUT | 更新角色 | role:edit |
| `/api/v1/roles/{role_name}` | DELETE | 删除角色 | role:delete |
| `/api/v1/roles/permissions` | GET | 权限列表 | role:list |

### 3.2 修改文件

#### 3.2.1 src/persistence/database.py

新增方法：
- `get_user_by_email(email)` - 按邮箱查询用户
- `save_user_with_password(user)` - 创建带密码的用户
- `update_user_password(user_id, password_hash)` - 更新密码
- `reset_user_password(user_id, password_hash)` - 重置密码
- `increment_login_attempts(user_id)` - 增加登录失败计数
- `reset_login_attempts(user_id)` - 重置登录失败计数
- `lock_user_account(user_id, minutes)` - 锁定账户
- `update_last_login(user_id, ip)` - 更新最后登录
- `log_login_audit(...)` - 记录登录审计
- `get_user_permissions(user_id)` - 获取用户权限
- `list_roles()` - 列出角色
- `get_role(role_name)` - 获取角色
- `create_role(...)` - 创建角色
- `update_role(...)` - 更新角色
- `delete_role(role_name)` - 删除角色
- `list_permissions(category)` - 列出权限
- `assign_user_role(...)` - 分配用户角色
- `update_user_roles(...)` - 更新用户角色
- `list_users_paginated(...)` - 分页查询用户

#### 3.2.2 src/api/router.py

```python
from .v1.auth import router as auth_router
from .v1.users import router as users_router
from .v1.roles import router as roles_router

api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(roles_router)
```

#### 3.2.3 requirements.txt

```
bcrypt>=4.0.0
```

---

## 4. 前端设计

### 4.1 新增文件

#### 4.1.1 web/src/store/useAuthStore.ts

Zustand状态管理：
```typescript
interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  setAuth: (user: User, token: string) => void;
  logout: () => void;
  hasPermission: (permission: string) => boolean;
  hasAnyPermission: (permissions: string[]) => boolean;
}
```

特性：
- 使用persist中间件持久化到localStorage
- 支持通配符权限检查 (admin:*)
- 支持前缀权限检查 (console:*)

#### 4.1.2 web/src/api/auth.ts

认证API客户端：
- `login(email, password)` - 登录
- `logout()` - 登出
- `changePassword(current, new, confirm)` - 改密码
- `getCurrentUser()` - 获取当前用户
- `setAuthToken(token)` - 设置请求头Token

#### 4.1.3 web/src/api/users.ts

用户管理API客户端：
- `listUsers(params)` - 用户列表
- `createUser(data)` - 创建用户
- `getUser(userId)` - 获取用户
- `updateUser(userId, data)` - 更新用户
- `deleteUser(userId)` - 删除用户
- `resetPassword(userId)` - 重置密码
- `listRoles()` - 角色列表
- `listPermissions()` - 权限列表

#### 4.1.4 web/src/pages/Login.tsx

登录页面：
- 邮箱输入（域名验证）
- 密码输入
- 登录按钮
- 错误提示
- 登录成功后检查force_password_change

#### 4.1.5 web/src/pages/UserManagement.tsx

用户管理页面（需user:list权限）：
- 用户列表表格
- 搜索过滤
- 创建用户对话框
- 编辑用户
- 重置密码
- 角色分配

#### 4.1.6 web/src/components/ProtectedRoute.tsx

路由守卫组件：
```tsx
<ProtectedRoute requiredPermissions={["console:services:view"]}>
  <ServicesPage />
</ProtectedRoute>
```

功能：
- 未认证跳转登录页
- 强制改密跳转改密页
- 权限不足跳转403页

#### 4.1.7 web/src/components/PasswordChangeModal.tsx

强制改密模态框：
- 当前密码输入
- 新密码输入
- 确认密码输入
- 密码强度验证
- 提交改密

### 4.2 修改文件

#### 4.2.1 web/src/lib/api.ts

添加请求拦截器：
```typescript
api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      useAuthStore.getState().logout();
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);
```

#### 4.2.2 web/src/router.tsx

更新路由配置：
```tsx
<Routes>
  {/* 公开路由 */}
  <Route path="/login" element={<LoginPage />} />

  {/* 受保护路由 */}
  <Route element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>
    <Route path="/dashboard" element={
      <ProtectedRoute requiredPermissions={["console:dashboard:view"]}>
        <DashboardPage />
      </ProtectedRoute>
    } />
    <Route path="/users" element={
      <ProtectedRoute requiredPermissions={["user:list"]}>
        <UserManagementPage />
      </ProtectedRoute>
    } />
    {/* ... 其他路由 */}
  </Route>
</Routes>
```

#### 4.2.3 web/src/layouts/AppLayout.tsx

更新布局：
- 显示当前登录用户
- 添加登出按钮
- 根据权限显示/隐藏菜单项
- 添加用户管理菜单（需权限）

---

## 5. 页面权限映射

| 页面/功能 | 路由 | 所需权限 |
|----------|------|---------|
| 仪表盘 | /dashboard | console:dashboard:view |
| 服务管理 | /services | console:services:view |
| 服务编辑 | /services (编辑操作) | console:services:edit |
| 知识库 | /knowledge | knowledge:dataset:view |
| Confluence | /confluence | knowledge:confluence:manage |
| 智能对话 | /playground | conversation:playground:access |
| 任务管理 | /tasks | console:dashboard:view |
| 系统设置 | /settings | console:settings:view |
| 用户管理 | /users | user:list |

---

## 6. 实现步骤

### Phase 1: 数据库迁移 (优先级: 高)

1. 创建迁移脚本 `005_account_permission_system.sql`
2. 在开发环境执行迁移
3. 验证表结构和初始数据
4. 更新 `database/run_migration.py` 支持新迁移

### Phase 2: 后端核心实现 (优先级: 高)

1. 创建 `src/core/auth/password.py`
2. 实现 `src/api/v1/auth.py` (登录/登出/改密)
3. 增强 `src/persistence/database.py` 用户操作方法
4. 更新 `src/api/router.py` 注册路由
5. 添加 bcrypt 依赖

### Phase 3: 用户管理API (优先级: 高)

1. 实现 `src/api/v1/users.py`
2. 实现 `src/api/v1/roles.py`
3. 测试所有API端点

### Phase 4: 前端认证 (优先级: 高)

1. 创建 `useAuthStore.ts`
2. 创建 `api/auth.ts`
3. 更新 `lib/api.ts` 拦截器
4. 实现登录页面
5. 实现强制改密模态框
6. 实现路由守卫

### Phase 5: 前端管理页面 (优先级: 中)

1. 实现用户管理页面
2. 更新AppLayout集成认证
3. 更新路由配置
4. 更新菜单权限控制

### Phase 6: 测试与验证 (优先级: 高)

1. 邮箱域名验证测试
2. 密码强度验证测试
3. 首次登录强制改密测试
4. 登录失败锁定测试
5. 权限检查测试
6. Token过期处理测试

---

## 7. 关键文件清单

### 数据库
- `database/migrations/005_account_permission_system.sql` (新建)

### 后端
- `src/core/auth/password.py` (新建)
- `src/api/v1/auth.py` (新建)
- `src/api/v1/users.py` (新建)
- `src/api/v1/roles.py` (新建)
- `src/persistence/database.py` (修改)
- `src/api/router.py` (修改)
- `requirements.txt` (修改)

### 前端
- `web/src/store/useAuthStore.ts` (新建)
- `web/src/api/auth.ts` (新建)
- `web/src/api/users.ts` (新建)
- `web/src/pages/Login.tsx` (新建)
- `web/src/pages/UserManagement.tsx` (新建)
- `web/src/components/ProtectedRoute.tsx` (新建)
- `web/src/components/PasswordChangeModal.tsx` (新建)
- `web/src/lib/api.ts` (修改)
- `web/src/router.tsx` (修改)
- `web/src/layouts/AppLayout.tsx` (修改)

---

## 8. 安全考虑

1. **密码存储**: 使用bcrypt (cost factor 12) 哈希
2. **账户锁定**: 5次失败锁定30分钟
3. **JWT安全**: Token 24小时过期
4. **邮箱验证**: 限制 @hejazfs.com.au 域名
5. **权限检查**: 所有敏感操作需权限验证
6. **审计日志**: 记录所有登录/登出/密码变更

---

## 9. 后续扩展

1. **密码过期策略**: 可配置密码有效期
2. **双因素认证**: 可选的2FA支持
3. **SSO集成**: 与公司现有auth系统集成
4. **细粒度数据权限**: 按租户/数据集隔离
5. **操作审计**: 完整的操作日志
