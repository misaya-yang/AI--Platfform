# 模型管理功能设计

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将硬编码的模型配置改为数据库管理，支持动态注册厂商和模型

**Architecture:** 两层管理结构 - Provider（厂商）管理 API Key 和连接配置，Model（模型）管理具体模型参数

**Tech Stack:** PostgreSQL, FastAPI, React + Ant Design

---

## 1. 数据模型

### llm_providers 表（厂商管理）

```sql
CREATE TABLE llm_providers (
    provider_id VARCHAR(50) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    api_type VARCHAR(20) DEFAULT 'openai',  -- 'openai' | 'anthropic' | 'google'
    base_url VARCHAR(500),
    api_key_encrypted TEXT,
    is_enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, provider_id)
);
```

### llm_models 表（模型管理）

```sql
CREATE TABLE llm_models (
    model_id VARCHAR(100) NOT NULL,
    tenant_id VARCHAR(100) NOT NULL,
    provider_id VARCHAR(50) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    context_window INT DEFAULT 128000,
    max_output_tokens INT DEFAULT 4096,
    supports_vision BOOLEAN DEFAULT false,
    supports_tools BOOLEAN DEFAULT true,
    input_price_per_1k DECIMAL(10,6) DEFAULT 0,
    output_price_per_1k DECIMAL(10,6) DEFAULT 0,
    access_level VARCHAR(20) DEFAULT 'public',
    is_enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (tenant_id, model_id),
    FOREIGN KEY (tenant_id, provider_id) REFERENCES llm_providers(tenant_id, provider_id)
);
```

## 2. API 设计

### Provider API

```
GET    /api/v1/providers           # 列出所有厂商
POST   /api/v1/providers           # 创建厂商
GET    /api/v1/providers/{id}      # 获取厂商详情
PUT    /api/v1/providers/{id}      # 更新厂商
DELETE /api/v1/providers/{id}      # 删除厂商
POST   /api/v1/providers/{id}/test # 测试 API 连接
```

### Model API

```
GET    /api/v1/models              # 列出所有模型
POST   /api/v1/models              # 创建模型
GET    /api/v1/models/{id}         # 获取模型详情
PUT    /api/v1/models/{id}         # 更新模型
DELETE /api/v1/models/{id}         # 删除模型
PUT    /api/v1/models/{id}/toggle  # 启用/禁用模型
```

## 3. 前端 UI

### 服务管理页面改造

```
/services 页面
├── Tab: 服务管理（现有内容）
├── Tab: 厂商管理
│   ├── 厂商卡片列表
│   └── 添加厂商弹窗
└── Tab: 模型管理
    ├── 模型表格（支持按厂商过滤）
    └── 添加模型弹窗
```

## 4. 迁移策略

1. 自动创建内置厂商：openai, anthropic, deepseek, dashscope, google
2. 自动导入 DEFAULT_MODELS 中的所有模型
3. ModelRegistry 改为从数据库加载，保持 API 兼容
