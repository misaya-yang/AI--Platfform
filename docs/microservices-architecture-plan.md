# Agent Gateway 微服务化改造实施计划

> **版本**: v1.0
> **日期**: 2026-01-06
> **作者**: Claude AI Architect
> **状态**: 待审批

---

## 目录

1. [执行摘要](#1-执行摘要)
2. [当前架构分析](#2-当前架构分析)
3. [目标架构设计](#3-目标架构设计)
4. [服务详细定义](#4-服务详细定义)
5. [数据架构](#5-数据架构)
6. [通信设计 (gRPC)](#6-通信设计-grpc)
7. [分阶段迁移计划](#7-分阶段迁移计划)
8. [Docker Compose 部署](#8-docker-compose-部署)
9. [Kubernetes 演进路径](#9-kubernetes-演进路径)
10. [风险与缓解措施](#10-风险与缓解措施)
11. [参考资料](#11-参考资料)

---

## 1. 执行摘要

### 1.1 改造目标

将当前单体 FastAPI 应用拆分为 **4 个微服务**，实现：

- **故障隔离**: 单个服务故障不影响其他服务
- **独立扩展**: 按需扩展高负载服务（如知识库处理）
- **渐进迁移**: 保持向后兼容，逐步拆分
- **简化运维**: 4 服务架构平衡了隔离性和复杂度

### 1.2 技术选型

| 维度 | 选择 | 理由 |
|------|------|------|
| 服务粒度 | 4 服务 | 平衡隔离性与运维复杂度 |
| 服务通信 | gRPC | 高性能、强类型、跨语言支持 |
| 部署方式 | Docker Compose → K8s | 循序渐进，先容器化再编排 |
| 数据库 | PostgreSQL (逻辑分库) | 保持现有技术栈 |
| 缓存/消息 | Redis | 保持现有技术栈 |

### 1.3 服务划分概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                      外部请求 (Web/API Client)                        │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     1. Gateway Service (网关服务)                    │
│   职责: 路由、限流、熔断、认证代理、透明代理、流式响应               │
│   端口: 8000 (HTTP) / 50051 (gRPC)                                  │
└─────────────────────────────────────────────────────────────────────┘
        │               │               │               │
        │ gRPC          │ gRPC          │ gRPC          │ gRPC
        ▼               ▼               ▼               ▼
   ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐
   │   2.    │    │   3.    │    │   4.    │    │ Shared  │
   │Identity │    │  Core   │    │Knowledge│    │  Infra  │
   │ Service │    │ Service │    │ Service │    │         │
   │         │    │         │    │         │    │PostgreSQL
   │用户/权限 │    │对话/任务 │    │知识库/RAG│    │ Redis   │
   │API Key  │    │会话管理  │    │向量检索  │    │ Qdrant  │
   └─────────┘    └─────────┘    └─────────┘    └─────────┘
      :8001          :8002          :8003
     :50052         :50053         :50054
```

---

## 2. 当前架构分析

### 2.1 现有系统结构

```
Agent_Gateway/
├── src/
│   ├── main.py              # FastAPI 入口，所有中间件注册
│   ├── container.py         # 依赖注入容器，管理所有组件
│   ├── api/
│   │   └── v1/              # 18+ 个 API 路由模块
│   │       ├── auth.py      # 认证 API
│   │       ├── users.py     # 用户管理
│   │       ├── roles.py     # 角色权限
│   │       ├── invoke.py    # 同步调用
│   │       ├── stream.py    # 流式调用
│   │       ├── sessions.py  # 会话管理
│   │       ├── tasks.py     # 异步任务
│   │       ├── knowledge.py # 知识库
│   │       ├── confluence.py# Confluence 集成
│   │       ├── services.py  # 服务注册
│   │       └── ...
│   ├── core/
│   │   ├── gateway/         # 调度器、熔断、限流
│   │   ├── auth/            # JWT、API Key、RBAC
│   │   └── middleware/      # 各类中间件
│   ├── services/
│   │   ├── registry/        # 服务注册表
│   │   ├── session/         # 会话管理
│   │   ├── task/            # 任务队列
│   │   └── knowledge/       # 知识库服务
│   ├── adapters/            # LLM 适配器 (LangGraph, OpenAI, Dify...)
│   └── persistence/         # 数据库、Redis 存储
└── database/
    └── schema.sql           # 完整数据库结构
```

### 2.2 当前问题

| 问题 | 影响 | 严重程度 |
|------|------|----------|
| **单点故障** | Knowledge Worker 崩溃导致整个服务不可用 | 高 |
| **资源竞争** | 文档向量化占用大量 CPU/内存，影响 API 响应 | 高 |
| **扩展困难** | 无法单独扩展高负载模块 | 中 |
| **部署风险** | 任何改动都需完整部署和测试 | 中 |
| **代码耦合** | 组件间直接依赖，难以独立开发 | 中 |

### 2.3 依赖关系图

```
                    ┌─────────────────┐
                    │   main.py       │
                    │  (FastAPI App)  │
                    └────────┬────────┘
                             │ 创建
                             ▼
                    ┌─────────────────┐
                    │  Container      │
                    │ (DI 容器)       │
                    └────────┬────────┘
                             │ 注入
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ GatewayDispatcher│   │SessionManager │   │TaskManager    │
└───────┬───────┘   └───────┬───────┘   └───────┬───────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                           │ 共享
                           ▼
                ┌─────────────────────┐
                │   DatabaseStorage   │
                │   RedisStorage      │
                └─────────────────────┘
```

---

## 3. 目标架构设计

### 3.1 四服务架构

| 服务 | 职责 | 端口 | 扩展特性 |
|------|------|------|----------|
| **Gateway** | 路由、限流、熔断、认证代理、透明代理 | 8000/50051 | 高吞吐，水平扩展 |
| **Identity** | 用户管理、API Key、角色权限、租户 | 8001/50052 | 低负载，高一致性 |
| **Core** | 对话会话、LLM 调用、异步任务、服务注册 | 8002/50053 | 中等负载，水平扩展 |
| **Knowledge** | 知识库、文档处理、向量检索、Confluence | 8003/50054 | CPU/内存密集，独立扩展 |

### 3.2 架构原则

1. **单一职责**: 每个服务专注于一个业务领域
2. **数据所有权**: 每个服务拥有自己的数据，其他服务通过 API 访问
3. **故障隔离**: 服务间使用熔断器，防止级联故障
4. **无状态设计**: Gateway 和 Core 服务无状态，便于水平扩展
5. **事件驱动**: 非关键路径使用 Redis 消息队列异步通信

### 3.3 代码迁移映射

#### Gateway Service
```
从当前代码库迁移:
├── src/main.py (部分 - 中间件和路由)
├── src/core/middleware/* (全部)
├── src/core/gateway/rate_limiter.py
├── src/core/gateway/circuit_breaker.py
├── src/proxy/* (全部)
└── src/api/v1/proxy.py, health.py, config.py
```

#### Identity Service
```
从当前代码库迁移:
├── src/api/v1/auth.py
├── src/api/v1/users.py
├── src/api/v1/roles.py
├── src/core/auth/* (JWT, API Key, RBAC, user_resolver)
└── src/persistence/repositories/user_repository.py, api_key_repository.py
```

#### Core Service
```
从当前代码库迁移:
├── src/api/v1/invoke.py, stream.py, submit.py
├── src/api/v1/sessions.py, conversations.py
├── src/api/v1/tasks.py
├── src/api/v1/services.py, langgraph.py
├── src/core/gateway/dispatcher.py, validator.py
├── src/services/registry/* (全部)
├── src/services/session/* (全部)
├── src/services/task/* (全部)
└── src/adapters/* (全部 LLM 适配器)
```

#### Knowledge Service
```
从当前代码库迁移:
├── src/api/v1/knowledge.py
├── src/api/v1/confluence.py
├── src/services/knowledge/* (全部)
└── src/persistence/repositories/knowledge_repository.py
```

---

## 4. 服务详细定义

### 4.1 Gateway Service (网关服务)

**核心职责**:
- HTTP/gRPC 请求路由
- JWT/API Key 验证（调用 Identity Service）
- 全局限流和熔断
- 请求日志和追踪
- 透明代理功能
- SSE/WebSocket 流式响应代理

**API 端点**:
```
HTTP:
  GET  /health, /health/live, /health/ready
  GET  /metrics
  ANY  /api/v1/proxy/*        # 透明代理
  ANY  /api/v1/*              # 路由到后端服务

gRPC:
  service GatewayService {
    rpc HealthCheck(Empty) returns (HealthResponse);
    rpc GetMetrics(Empty) returns (MetricsResponse);
  }
```

**依赖**:
- Identity Service (gRPC) - Token 验证
- Core Service (gRPC/HTTP) - 请求转发
- Knowledge Service (gRPC/HTTP) - 请求转发
- Redis - 限流计数器、熔断状态

**配置示例**:
```yaml
gateway:
  host: 0.0.0.0
  http_port: 8000
  grpc_port: 50051

  # 服务发现
  services:
    identity:
      host: identity
      grpc_port: 50052
    core:
      host: core
      http_port: 8002
      grpc_port: 50053
    knowledge:
      host: knowledge
      http_port: 8003
      grpc_port: 50054

  # 限流配置
  rate_limit:
    global:
      requests: 1000
      window_seconds: 60
    per_user:
      requests: 100
      window_seconds: 60

  # 熔断配置
  circuit_breaker:
    failure_threshold: 5
    recovery_timeout: 30
```

---

### 4.2 Identity Service (身份服务)

**核心职责**:
- 用户认证（登录/登出/密码重置）
- 用户管理（CRUD）
- API Key 管理
- 角色权限（RBAC）
- 租户管理

**API 端点**:
```
HTTP:
  POST /api/v1/auth/login
  POST /api/v1/auth/logout
  POST /api/v1/auth/change-password
  GET  /api/v1/users
  POST /api/v1/users
  GET  /api/v1/users/{user_id}
  PUT  /api/v1/users/{user_id}
  DELETE /api/v1/users/{user_id}
  GET  /api/v1/roles
  POST /api/v1/roles
  ...

gRPC (供 Gateway 调用):
  service IdentityService {
    rpc ValidateToken(ValidateTokenRequest) returns (ValidateTokenResponse);
    rpc GetUser(GetUserRequest) returns (User);
    rpc CheckPermission(CheckPermissionRequest) returns (CheckPermissionResponse);
    rpc ListUserRoles(ListUserRolesRequest) returns (ListUserRolesResponse);
  }
```

**数据表**:
```sql
-- Identity Service 拥有的表
users, api_keys, tenants, rbac_roles, permissions,
user_roles, user_permissions, auth_config, login_audit
```

**Proto 定义**:
```protobuf
// identity.proto
syntax = "proto3";
package identity;

service IdentityService {
  rpc ValidateToken(ValidateTokenRequest) returns (ValidateTokenResponse);
  rpc GetUser(GetUserRequest) returns (User);
  rpc CheckPermission(CheckPermissionRequest) returns (CheckPermissionResponse);
}

message ValidateTokenRequest {
  string token = 1;
  string token_type = 2; // "jwt" | "api_key"
}

message ValidateTokenResponse {
  bool valid = 1;
  string user_id = 2;
  string tenant_id = 3;
  repeated string roles = 4;
  string tier = 5;
  repeated string permissions = 6;
  string error = 7;
}

message User {
  string user_id = 1;
  string username = 2;
  string email = 3;
  string display_name = 4;
  string tenant_id = 5;
  string tier = 6;
  repeated string roles = 7;
  string status = 8;
  google.protobuf.Timestamp created_at = 9;
  google.protobuf.Timestamp updated_at = 10;
}

message GetUserRequest {
  string user_id = 1;
}

message CheckPermissionRequest {
  string user_id = 1;
  string permission = 2;
  string resource_type = 3;
  string resource_id = 4;
}

message CheckPermissionResponse {
  bool allowed = 1;
  string reason = 2;
}
```

---

### 4.3 Core Service (核心服务)

**核心职责**:
- 对话会话管理（创建、获取、更新、删除）
- 多轮对话上下文
- LLM 调用分发（同步/流式）
- 异步任务队列
- 服务注册与发现
- LangGraph 代理

**API 端点**:
```
HTTP:
  # 对话 API
  POST /api/v1/invoke              # 同步调用
  POST /api/v1/stream              # 流式调用 (SSE)
  POST /api/v1/submit              # 异步提交
  GET  /api/v1/conversations       # 对话列表
  POST /api/v1/conversations       # 创建对话

  # 会话 API
  GET  /api/v1/sessions
  POST /api/v1/sessions
  GET  /api/v1/sessions/{session_id}
  DELETE /api/v1/sessions/{session_id}
  GET  /api/v1/sessions/{session_id}/messages

  # 任务 API
  GET  /api/v1/tasks
  GET  /api/v1/tasks/{task_id}
  DELETE /api/v1/tasks/{task_id}

  # 服务注册 API
  GET  /api/v1/services
  POST /api/v1/services
  GET  /api/v1/services/{service_id}
  PUT  /api/v1/services/{service_id}
  DELETE /api/v1/services/{service_id}

  # LangGraph API
  POST /api/v1/langgraph/threads
  POST /api/v1/langgraph/runs/stream

gRPC:
  service CoreService {
    rpc Invoke(InvokeRequest) returns (InvokeResponse);
    rpc Stream(StreamRequest) returns (stream StreamChunk);
    rpc GetSession(GetSessionRequest) returns (Session);
    rpc GetService(GetServiceRequest) returns (ServiceDefinition);
  }
```

**数据表**:
```sql
-- Core Service 拥有的表
sessions, langgraph_threads, tasks, services,
service_health_records, rate_limit_config, semantic_cache
```

**适配器集成**:
```
Core Service 内部集成所有 LLM 适配器:
├── LangGraphAdapter
├── OpenAIAdapter
├── DifyAdapter
├── ComfyUIAdapter
├── WhisperAdapter
├── TTSAdapter
├── StableDiffusionAdapter
└── GenericRESTAdapter
```

---

### 4.4 Knowledge Service (知识库服务)

**核心职责**:
- 数据集管理（创建、配置、删除）
- 文档上传与解析
- 文本分块（Chunking）
- 向量嵌入（Embedding）
- 语义检索（Retrieval）
- Confluence 集成与同步
- Q&A 服务

**API 端点**:
```
HTTP:
  # 数据集 API
  GET  /api/v1/knowledge/datasets
  POST /api/v1/knowledge/datasets
  GET  /api/v1/knowledge/datasets/{dataset_id}
  PUT  /api/v1/knowledge/datasets/{dataset_id}
  DELETE /api/v1/knowledge/datasets/{dataset_id}

  # 文档 API
  GET  /api/v1/knowledge/datasets/{dataset_id}/documents
  POST /api/v1/knowledge/datasets/{dataset_id}/documents
  GET  /api/v1/knowledge/datasets/{dataset_id}/documents/{doc_id}
  DELETE /api/v1/knowledge/datasets/{dataset_id}/documents/{doc_id}

  # 检索 API
  POST /api/v1/knowledge/datasets/{dataset_id}/retrieve
  POST /api/v1/knowledge/qa  # 问答接口

  # Confluence API
  GET  /api/v1/confluence/connections
  POST /api/v1/confluence/connections
  POST /api/v1/confluence/connections/{id}/sync

gRPC:
  service KnowledgeService {
    rpc Retrieve(RetrieveRequest) returns (RetrieveResponse);
    rpc QA(QARequest) returns (QAResponse);
    rpc GetDataset(GetDatasetRequest) returns (Dataset);
  }
```

**数据表**:
```sql
-- Knowledge Service 拥有的表
datasets, documents, segments, dataset_permissions,
confluence_connections, confluence_spaces
```

**外部依赖**:
- Qdrant (向量数据库)
- Embedding API (OpenAI/本地模型)

---

## 5. 数据架构

### 5.1 数据库分库策略

**阶段一 (逻辑分库)**: 使用 PostgreSQL Schema 隔离

```sql
-- 创建各服务 Schema
CREATE SCHEMA identity;
CREATE SCHEMA core;
CREATE SCHEMA knowledge;

-- 迁移表到对应 Schema
ALTER TABLE users SET SCHEMA identity;
ALTER TABLE api_keys SET SCHEMA identity;
ALTER TABLE tenants SET SCHEMA identity;
ALTER TABLE rbac_roles SET SCHEMA identity;

ALTER TABLE sessions SET SCHEMA core;
ALTER TABLE tasks SET SCHEMA core;
ALTER TABLE services SET SCHEMA core;
ALTER TABLE langgraph_threads SET SCHEMA core;

ALTER TABLE datasets SET SCHEMA knowledge;
ALTER TABLE documents SET SCHEMA knowledge;
ALTER TABLE segments SET SCHEMA knowledge;
ALTER TABLE dataset_permissions SET SCHEMA knowledge;
```

**阶段二 (物理分库)**: 独立数据库实例 (K8s 阶段)

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│ db_identity │  │  db_core    │  │db_knowledge │
│             │  │             │  │             │
│ users       │  │ sessions    │  │ datasets    │
│ api_keys    │  │ tasks       │  │ documents   │
│ tenants     │  │ services    │  │ segments    │
│ rbac_roles  │  │ lg_threads  │  │ permissions │
└─────────────┘  └─────────────┘  └─────────────┘
```

### 5.2 数据所有权矩阵

| 表名 | 所属服务 | 访问模式 | 说明 |
|------|----------|----------|------|
| users | Identity | Read/Write | 用户主数据 |
| api_keys | Identity | Read/Write | API Key 管理 |
| tenants | Identity | Read/Write | 租户数据 |
| rbac_roles | Identity | Read/Write | 角色定义 |
| permissions | Identity | Read | Gateway 读取验证 |
| sessions | Core | Read/Write | 会话状态 |
| tasks | Core | Read/Write | 异步任务 |
| services | Core | Read/Write | 服务注册 |
| langgraph_threads | Core | Read/Write | Thread 映射 |
| datasets | Knowledge | Read/Write | 数据集元数据 |
| documents | Knowledge | Read/Write | 文档元数据 |
| segments | Knowledge | Read/Write | 文本分片 |
| audit_logs | 共享写入 | Append | 各服务写入 |
| usage_statistics | 共享写入 | Append | 各服务上报 |

### 5.3 跨服务数据访问

```
┌─────────────────────────────────────────────────────────────────┐
│                        数据访问模式                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Gateway ─────gRPC────> Identity.ValidateToken()                │
│     │                                                            │
│     │  (验证通过后，携带 user_context 转发请求)                   │
│     │                                                            │
│     └─────HTTP/gRPC────> Core.Invoke()                          │
│                             │                                    │
│                             │ (如需检索知识库)                    │
│                             │                                    │
│                             └────gRPC────> Knowledge.Retrieve()  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 6. 通信设计 (gRPC)

### 6.1 Proto 文件结构

```
shared/
└── proto/
    ├── common.proto       # 公共消息定义
    ├── identity.proto     # 身份服务接口
    ├── core.proto         # 核心服务接口
    └── knowledge.proto    # 知识库服务接口
```

### 6.2 common.proto

```protobuf
syntax = "proto3";
package common;

import "google/protobuf/timestamp.proto";

// 用户上下文 - 在所有请求中传递
message UserContext {
  string user_id = 1;
  string tenant_id = 2;
  repeated string roles = 3;
  string tier = 4;
  repeated string permissions = 5;
  bool is_authenticated = 6;
  bool is_anonymous = 7;
}

// 分页请求
message PaginationRequest {
  int32 page = 1;
  int32 page_size = 2;
}

// 分页响应
message PaginationResponse {
  int32 total = 1;
  int32 page = 2;
  int32 page_size = 3;
  int32 total_pages = 4;
}

// 错误响应
message Error {
  string code = 1;
  string message = 2;
  map<string, string> details = 3;
}
```

### 6.3 服务间调用流程

```
┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  Client  │    │ Gateway  │    │ Identity │    │   Core   │
└────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘
     │               │               │               │
     │ HTTP Request  │               │               │
     │ Authorization:│               │               │
     │ Bearer <JWT>  │               │               │
     │──────────────>│               │               │
     │               │               │               │
     │               │ gRPC: ValidateToken(JWT)     │
     │               │──────────────>│               │
     │               │               │               │
     │               │ ValidateTokenResponse        │
     │               │ (user_id, roles, permissions)│
     │               │<──────────────│               │
     │               │               │               │
     │               │ gRPC: Invoke(request, user_context)
     │               │──────────────────────────────>│
     │               │               │               │
     │               │ InvokeResponse / Stream      │
     │               │<──────────────────────────────│
     │               │               │               │
     │ HTTP Response │               │               │
     │<──────────────│               │               │
     │               │               │               │
```

### 6.4 gRPC 配置

```python
# 客户端配置示例
import grpc
from grpc import aio

class IdentityClient:
    def __init__(self, host: str, port: int):
        self.channel = aio.insecure_channel(f"{host}:{port}")
        self.stub = identity_pb2_grpc.IdentityServiceStub(self.channel)

    async def validate_token(self, token: str, token_type: str) -> UserContext:
        request = identity_pb2.ValidateTokenRequest(
            token=token,
            token_type=token_type
        )
        response = await self.stub.ValidateToken(request)
        return response

# 服务端配置示例
async def serve():
    server = aio.server()
    identity_pb2_grpc.add_IdentityServiceServicer_to_server(
        IdentityServicer(), server
    )
    server.add_insecure_port('[::]:50052')
    await server.start()
    await server.wait_for_termination()
```

---

## 7. 分阶段迁移计划

### 7.1 阶段概览

| 阶段 | 目标 | 交付物 |
|------|------|--------|
| Phase 0 | 基础设施准备 | Docker Compose, 共享库, Proto 定义 |
| Phase 1 | 提取 Identity Service | 独立身份服务 |
| Phase 2 | 提取 Knowledge Service | 独立知识库服务 |
| Phase 3 | 提取 Core Service | 独立核心服务 |
| Phase 4 | Gateway 精简 | 纯路由网关 |
| Phase 5 | 生产优化 | 监控、日志、CI/CD |

### 7.2 Phase 0: 基础设施准备

**目标**: 建立微服务开发所需的基础设施和共享组件

**任务清单**:

1. **创建 Monorepo 结构**
```
agent-gateway-platform/
├── services/
│   ├── gateway/
│   ├── identity/
│   ├── core/
│   └── knowledge/
├── shared/
│   ├── proto/
│   ├── python/
│   │   └── gateway_common/
│   └── docker/
├── infra/
│   ├── docker-compose.yml
│   └── docker-compose.dev.yml
└── database/
    └── migrations/
```

2. **编写 Proto 定义文件**
   - `shared/proto/common.proto`
   - `shared/proto/identity.proto`
   - `shared/proto/core.proto`
   - `shared/proto/knowledge.proto`

3. **创建共享 Python 包** (`gateway_common`)
   - 日志配置
   - 追踪中间件
   - gRPC 客户端基类
   - 公共数据模型

4. **准备 Docker Compose 文件**
   - 基础设施服务 (PostgreSQL, Redis, Qdrant)
   - 开发环境配置
   - 网络定义

**关键文件**:
- `shared/proto/*.proto`
- `shared/python/gateway_common/`
- `infra/docker-compose.yml`

---

### 7.3 Phase 1: 提取 Identity Service

**目标**: 将身份认证功能拆分为独立服务

**为什么首先拆分**:
- 边界最清晰，依赖最少
- 安全隔离收益最大
- 验证微服务架构可行性

**迁移步骤**:

1. **创建 Identity Service 项目**
```
services/identity/
├── src/
│   ├── main.py           # FastAPI + gRPC 入口
│   ├── api/
│   │   ├── auth.py       # 从 src/api/v1/auth.py 迁移
│   │   ├── users.py      # 从 src/api/v1/users.py 迁移
│   │   └── roles.py      # 从 src/api/v1/roles.py 迁移
│   ├── grpc/
│   │   ├── server.py     # gRPC 服务实现
│   │   └── interceptors.py
│   ├── core/
│   │   ├── jwt.py        # 从 src/core/auth/jwt.py 迁移
│   │   ├── api_key.py    # 从 src/core/auth/api_key.py 迁移
│   │   └── rbac.py       # 从 src/core/auth/rbac.py 迁移
│   └── repositories/
│       ├── user_repository.py
│       └── api_key_repository.py
├── Dockerfile
├── pyproject.toml
└── config.yaml
```

2. **实现 gRPC 接口**
```python
# services/identity/src/grpc/server.py
from generated import identity_pb2, identity_pb2_grpc

class IdentityServicer(identity_pb2_grpc.IdentityServiceServicer):
    async def ValidateToken(self, request, context):
        # 验证 JWT 或 API Key
        if request.token_type == "jwt":
            payload = await self.jwt_service.verify(request.token)
        else:
            payload = await self.api_key_service.verify(request.token)

        return identity_pb2.ValidateTokenResponse(
            valid=True,
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            roles=payload.roles,
            tier=payload.tier,
            permissions=payload.permissions
        )
```

3. **更新 Gateway 使用 gRPC 客户端**
```python
# 在原 main.py 中添加
from gateway_common.clients import IdentityClient

# 认证中间件改为调用 Identity Service
class AuthMiddleware:
    def __init__(self, identity_client: IdentityClient):
        self.identity_client = identity_client

    async def __call__(self, request, call_next):
        token = self.extract_token(request)
        if token:
            user_ctx = await self.identity_client.validate_token(token)
            request.state.user = user_ctx
        return await call_next(request)
```

4. **数据库迁移**
```sql
-- 创建 identity schema
CREATE SCHEMA identity;

-- 迁移表
ALTER TABLE users SET SCHEMA identity;
ALTER TABLE api_keys SET SCHEMA identity;
ALTER TABLE tenants SET SCHEMA identity;
ALTER TABLE rbac_roles SET SCHEMA identity;
```

5. **配置路由**
```yaml
# Gateway 路由配置
routes:
  - path: /api/v1/auth/*
    service: identity
    port: 8001
  - path: /api/v1/users/*
    service: identity
    port: 8001
  - path: /api/v1/roles/*
    service: identity
    port: 8001
```

**验收标准**:
- [ ] Identity Service 独立运行
- [ ] Gateway 通过 gRPC 验证 Token
- [ ] 所有认证/用户 API 正常工作
- [ ] 原单体应用可以降级运行

---

### 7.4 Phase 2: 提取 Knowledge Service

**目标**: 将知识库功能拆分为独立服务

**为什么第二个拆分**:
- CPU/内存密集型，独立扩展收益大
- 与其他模块耦合较少
- 可以独立处理文档向量化

**迁移步骤**:

1. **创建 Knowledge Service 项目**
```
services/knowledge/
├── src/
│   ├── main.py
│   ├── api/
│   │   ├── datasets.py
│   │   ├── documents.py
│   │   ├── retrieval.py
│   │   └── confluence.py
│   ├── grpc/
│   │   └── server.py
│   ├── services/
│   │   ├── knowledge_service.py
│   │   ├── chunking.py
│   │   ├── embedding.py
│   │   ├── retrieval.py
│   │   └── vector_store.py
│   ├── workers/
│   │   ├── document_worker.py
│   │   └── confluence_sync.py
│   └── repositories/
│       └── knowledge_repository.py
├── Dockerfile
└── pyproject.toml
```

2. **Worker 进程分离**
```python
# 独立的 Document Worker
# services/knowledge/src/workers/document_worker.py
class DocumentWorker:
    def __init__(self, redis_client, knowledge_service):
        self.redis = redis_client
        self.service = knowledge_service

    async def run(self):
        while True:
            task = await self.redis.blpop('knowledge:tasks')
            await self.process_document(task)

    async def process_document(self, task):
        # 解析 -> 分块 -> 向量化 -> 存储
        doc = await self.service.parse_document(task.document_id)
        chunks = await self.service.chunk_document(doc)
        await self.service.embed_and_store(chunks)
```

3. **更新 Core Service 调用 Knowledge**
```python
# Core Service 中的 RAG 调用
class KnowledgeClient:
    async def retrieve(self, dataset_id: str, query: str, top_k: int = 5):
        request = knowledge_pb2.RetrieveRequest(
            dataset_id=dataset_id,
            query=query,
            top_k=top_k
        )
        return await self.stub.Retrieve(request)
```

**验收标准**:
- [ ] Knowledge Service 独立运行
- [ ] 文档处理 Worker 独立进程
- [ ] 检索 API 正常工作
- [ ] Confluence 同步正常

---

### 7.5 Phase 3: 提取 Core Service

**目标**: 将核心对话和任务功能拆分

**迁移步骤**:

1. **创建 Core Service 项目**
```
services/core/
├── src/
│   ├── main.py
│   ├── api/
│   │   ├── invoke.py
│   │   ├── stream.py
│   │   ├── sessions.py
│   │   ├── tasks.py
│   │   ├── services.py
│   │   └── langgraph.py
│   ├── grpc/
│   │   └── server.py
│   ├── services/
│   │   ├── dispatcher.py
│   │   ├── session_manager.py
│   │   ├── task_manager.py
│   │   └── service_registry.py
│   ├── adapters/
│   │   ├── langgraph.py
│   │   ├── openai.py
│   │   ├── dify.py
│   │   └── ...
│   └── workers/
│       └── task_worker.py
├── Dockerfile
└── pyproject.toml
```

2. **更新 Gateway 路由**
```python
# Gateway 请求转发
@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_to_core(request: Request, path: str):
    # 验证 Token
    user_ctx = await identity_client.validate_token(...)

    # 转发到 Core Service
    async with httpx.AsyncClient() as client:
        response = await client.request(
            method=request.method,
            url=f"http://core:8002/api/v1/{path}",
            headers={**request.headers, "X-User-Context": user_ctx.json()},
            content=await request.body()
        )
    return Response(content=response.content, status_code=response.status_code)
```

**验收标准**:
- [ ] Core Service 独立运行
- [ ] 会话管理正常
- [ ] 流式响应正常
- [ ] 异步任务正常
- [ ] LLM 适配器全部迁移

---

### 7.6 Phase 4: Gateway 精简

**目标**: Gateway 只保留路由和代理功能

**最终 Gateway 结构**:
```
services/gateway/
├── src/
│   ├── main.py
│   ├── middleware/
│   │   ├── auth.py           # Token 验证 (调用 Identity)
│   │   ├── rate_limit.py     # 限流
│   │   ├── circuit_breaker.py# 熔断
│   │   └── logging.py        # 请求日志
│   ├── proxy/
│   │   ├── router.py         # 路由规则
│   │   └── transparent.py    # 透明代理
│   └── grpc/
│       └── clients.py        # gRPC 客户端
├── Dockerfile
└── pyproject.toml
```

**验收标准**:
- [ ] Gateway 无业务逻辑
- [ ] 所有请求正确路由
- [ ] 限流熔断正常
- [ ] 透明代理正常

---

### 7.7 Phase 5: 生产优化

**目标**: 生产环境准备

**任务清单**:
1. 集中日志收集 (Loki/ELK)
2. 分布式追踪 (Jaeger)
3. 监控告警 (Prometheus + Grafana)
4. CI/CD 流水线
5. Kubernetes 迁移准备

---

## 8. Docker Compose 部署

### 8.1 完整配置文件

```yaml
# infra/docker-compose.yml
version: '3.8'

networks:
  gateway-network:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  qdrant_data:

services:
  # ============ 基础设施 ============

  postgres:
    image: postgres:15-alpine
    container_name: gateway-postgres
    environment:
      POSTGRES_DB: agent_gateway
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./database/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "5432:5432"
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: gateway-redis
    command: redis-server --appendonly yes
    volumes:
      - redis_data:/data
    ports:
      - "6379:6379"
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  qdrant:
    image: qdrant/qdrant:latest
    container_name: gateway-qdrant
    volumes:
      - qdrant_data:/qdrant/storage
    ports:
      - "6333:6333"
      - "6334:6334"
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  # ============ 微服务 ============

  gateway:
    build:
      context: ./services/gateway
      dockerfile: Dockerfile
    container_name: gateway-gateway
    ports:
      - "8000:8000"      # HTTP
      - "50051:50051"    # gRPC
    environment:
      - HOST=0.0.0.0
      - HTTP_PORT=8000
      - GRPC_PORT=50051
      - IDENTITY_GRPC_HOST=identity
      - IDENTITY_GRPC_PORT=50052
      - CORE_HTTP_HOST=core
      - CORE_HTTP_PORT=8002
      - CORE_GRPC_HOST=core
      - CORE_GRPC_PORT=50053
      - KNOWLEDGE_HTTP_HOST=knowledge
      - KNOWLEDGE_HTTP_PORT=8003
      - REDIS_URL=redis://redis:6379
    depends_on:
      redis:
        condition: service_healthy
      identity:
        condition: service_healthy
      core:
        condition: service_healthy
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  identity:
    build:
      context: ./services/identity
      dockerfile: Dockerfile
    container_name: gateway-identity
    ports:
      - "8001:8001"      # HTTP
      - "50052:50052"    # gRPC
    environment:
      - HOST=0.0.0.0
      - HTTP_PORT=8001
      - GRPC_PORT=50052
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/agent_gateway
      - DATABASE_SCHEMA=identity
      - REDIS_URL=redis://redis:6379
      - JWT_SECRET=${JWT_SECRET:-your-secret-key}
      - JWT_ALGORITHM=HS256
      - JWT_EXPIRE_MINUTES=1440
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  core:
    build:
      context: ./services/core
      dockerfile: Dockerfile
    container_name: gateway-core
    ports:
      - "8002:8002"      # HTTP
      - "50053:50053"    # gRPC
    environment:
      - HOST=0.0.0.0
      - HTTP_PORT=8002
      - GRPC_PORT=50053
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/agent_gateway
      - DATABASE_SCHEMA=core
      - REDIS_URL=redis://redis:6379
      - KNOWLEDGE_GRPC_HOST=knowledge
      - KNOWLEDGE_GRPC_PORT=50054
      - LANGGRAPH_URLS=${LANGGRAPH_URLS:-}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8002/health"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  core-worker:
    build:
      context: ./services/core
      dockerfile: Dockerfile
    container_name: gateway-core-worker
    command: ["python", "-m", "src.workers.task_worker"]
    environment:
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/agent_gateway
      - DATABASE_SCHEMA=core
      - REDIS_URL=redis://redis:6379
      - WORKER_CONCURRENCY=4
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    networks:
      - gateway-network
    restart: unless-stopped

  knowledge:
    build:
      context: ./services/knowledge
      dockerfile: Dockerfile
    container_name: gateway-knowledge
    ports:
      - "8003:8003"      # HTTP
      - "50054:50054"    # gRPC
    environment:
      - HOST=0.0.0.0
      - HTTP_PORT=8003
      - GRPC_PORT=50054
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/agent_gateway
      - DATABASE_SCHEMA=knowledge
      - REDIS_URL=redis://redis:6379
      - QDRANT_URL=http://qdrant:6333
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - EMBEDDING_MODEL=text-embedding-3-small
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      qdrant:
        condition: service_healthy
    networks:
      - gateway-network
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8003/health"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  knowledge-worker:
    build:
      context: ./services/knowledge
      dockerfile: Dockerfile
    container_name: gateway-knowledge-worker
    command: ["python", "-m", "src.workers.document_worker"]
    environment:
      - DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD:-postgres}@postgres:5432/agent_gateway
      - DATABASE_SCHEMA=knowledge
      - REDIS_URL=redis://redis:6379
      - QDRANT_URL=http://qdrant:6333
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - EMBEDDING_MODEL=text-embedding-3-small
      - WORKER_CONCURRENCY=2
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      qdrant:
        condition: service_healthy
    networks:
      - gateway-network
    restart: unless-stopped
```

### 8.2 环境变量文件

```bash
# .env
POSTGRES_PASSWORD=your_secure_password
JWT_SECRET=your_jwt_secret_key_at_least_32_chars
OPENAI_API_KEY=sk-your-openai-api-key
LANGGRAPH_URLS=http://langgraph-1:8123,http://langgraph-2:8123
```

### 8.3 启动命令

```bash
# 开发环境
docker-compose -f docker-compose.yml up -d

# 查看日志
docker-compose logs -f gateway

# 扩展 Worker
docker-compose up -d --scale core-worker=4 --scale knowledge-worker=2

# 停止
docker-compose down
```

---

## 9. Kubernetes 演进路径

### 9.1 迁移时机

当满足以下条件时，考虑迁移到 Kubernetes:
- 需要自动扩缩容
- 需要滚动更新/蓝绿部署
- 需要更细粒度的资源限制
- 需要服务网格 (Service Mesh)

### 9.2 K8s 资源规划

| 服务 | Replicas | CPU Request | CPU Limit | Memory Request | Memory Limit |
|------|----------|-------------|-----------|----------------|--------------|
| Gateway | 3-10 | 100m | 500m | 256Mi | 512Mi |
| Identity | 2-5 | 100m | 300m | 256Mi | 512Mi |
| Core | 2-10 | 250m | 1000m | 512Mi | 1Gi |
| Core Worker | 2-20 | 500m | 2000m | 1Gi | 2Gi |
| Knowledge | 2-5 | 250m | 1000m | 512Mi | 1Gi |
| Knowledge Worker | 1-10 | 1000m | 4000m | 2Gi | 4Gi |

### 9.3 Helm Chart 结构 (预留)

```
helm/
└── agent-gateway/
    ├── Chart.yaml
    ├── values.yaml
    ├── templates/
    │   ├── gateway/
    │   │   ├── deployment.yaml
    │   │   ├── service.yaml
    │   │   └── hpa.yaml
    │   ├── identity/
    │   ├── core/
    │   ├── knowledge/
    │   └── common/
    │       ├── configmap.yaml
    │       └── secrets.yaml
    └── charts/
        ├── postgresql/
        └── redis/
```

---

## 10. 风险与缓解措施

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| **数据不一致** | 中 | 高 | 使用事件驱动最终一致性；关键路径使用同步调用 |
| **网络延迟增加** | 高 | 中 | gRPC 高性能通信；积极缓存；连接池复用 |
| **部署复杂度** | 高 | 中 | Docker Compose 简化本地开发；CI/CD 自动化 |
| **调试困难** | 中 | 中 | 分布式追踪 (Jaeger)；集中日志 (Loki) |
| **服务发现失败** | 低 | 高 | Docker 内置 DNS；K8s Service 发现；熔断降级 |
| **Schema 迁移冲突** | 低 | 高 | 严格版本控制；向后兼容迁移；灰度发布 |

---

## 11. 参考资料

### 11.1 业界最佳实践

- [AI Orchestration: The Microservices Approach to LLMs](https://dev.to/maliano63717738/ai-orchestration-the-microservices-approach-to-large-language-models-4bj6)
- [5 Patterns for Scalable LLM Service Integration](https://latitude-blog.ghost.io/blog/5-patterns-for-scalable-llm-service-integration/)
- [In-Depth Analysis of AI Gateway](https://jimmysong.io/blog/ai-gateway-in-depth/)
- [LLM Orchestration in 2025](https://orq.ai/blog/llm-orchestration)
- [Kong AI Gateway](https://konghq.com/products/kong-ai-gateway)
- [Portkey AI Gateway](https://portkey.ai/features/ai-gateway)

### 11.2 技术文档

- [gRPC Python Documentation](https://grpc.io/docs/languages/python/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [FastAPI Microservices](https://fastapi.tiangolo.com/deployment/)

### 11.3 当前代码库关键文件

- `src/main.py` - 应用入口
- `src/container.py` - 依赖注入容器
- `src/api/router.py` - API 路由定义
- `src/core/gateway/dispatcher.py` - 核心调度逻辑
- `database/schema.sql` - 数据库结构

---

## 附录 A: 术语表

| 术语 | 定义 |
|------|------|
| gRPC | Google Remote Procedure Call，高性能 RPC 框架 |
| Proto | Protocol Buffers，gRPC 使用的接口定义语言 |
| Schema | PostgreSQL 逻辑分库机制 |
| Worker | 后台任务处理进程 |
| SSE | Server-Sent Events，服务器推送事件 |
| RAG | Retrieval-Augmented Generation，检索增强生成 |
| RBAC | Role-Based Access Control，基于角色的访问控制 |

---

## 附录 B: 检查清单

### Phase 0 完成检查
- [ ] Monorepo 结构创建完成
- [ ] Proto 文件定义完成
- [ ] 共享库 (`gateway_common`) 发布
- [ ] Docker Compose 基础设施运行正常
- [ ] 开发环境文档编写

### Phase 1 完成检查
- [ ] Identity Service 独立部署
- [ ] gRPC 接口测试通过
- [ ] Token 验证功能正常
- [ ] 用户管理 API 正常
- [ ] 数据库 Schema 迁移完成

### Phase 2 完成检查
- [ ] Knowledge Service 独立部署
- [ ] 文档处理 Worker 正常
- [ ] 向量检索功能正常
- [ ] Confluence 同步正常

### Phase 3 完成检查
- [ ] Core Service 独立部署
- [ ] 会话管理正常
- [ ] 流式响应正常
- [ ] 异步任务正常
- [ ] 所有 LLM 适配器迁移完成

### Phase 4 完成检查
- [ ] Gateway 精简完成
- [ ] 所有路由正常
- [ ] 限流熔断正常
- [ ] 性能测试通过

---

*文档结束*
