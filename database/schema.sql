-- ============================================================
-- Agent Gateway 数据库建表脚本
-- 数据库：PostgreSQL
-- 版本：2.0.0
-- ============================================================

SET client_encoding TO 'UTF8';

-- 可选扩展（需要足够权限）
DO $$
BEGIN
    BEGIN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'skip extension uuid-ossp: insufficient privilege';
    END;
    BEGIN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS "pgcrypto"';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'skip extension pgcrypto: insufficient privilege';
    END;
END
$$;

-- ============================================================
-- 1. 服务定义表
-- ============================================================
CREATE TABLE IF NOT EXISTS services (
    service_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    version VARCHAR(50) NOT NULL DEFAULT '1.0.0',
    service_type VARCHAR(50) NOT NULL DEFAULT 'custom',
    connector_type VARCHAR(50) NOT NULL DEFAULT 'http',
    connector_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    supported_modes VARCHAR(50)[] NOT NULL DEFAULT ARRAY['sync', 'stream']::VARCHAR(50)[],
    accepted_content_types VARCHAR(50)[] NOT NULL DEFAULT ARRAY['text']::VARCHAR(50)[],
    output_content_types VARCHAR(50)[] NOT NULL DEFAULT ARRAY['text']::VARCHAR(50)[],
    input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    session_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    session_adapter VARCHAR(100),
    timeout INTEGER NOT NULL DEFAULT 60 CHECK (timeout > 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries >= 0),
    retry_delay NUMERIC(5,2) NOT NULL DEFAULT 1.0 CHECK (retry_delay >= 0),
    circuit_breaker_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    failure_threshold INTEGER NOT NULL DEFAULT 5 CHECK (failure_threshold >= 1),
    recovery_timeout INTEGER NOT NULL DEFAULT 30 CHECK (recovery_timeout >= 0),
    rate_limit JSONB,
    concurrency_limit INTEGER CHECK (concurrency_limit IS NULL OR concurrency_limit >= 0),
    service_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    async_config JSONB,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    tags VARCHAR(100)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(100)[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE services IS '服务定义表：描述可被网关调用的 AI/工具服务及其连接与运行参数。';
COMMENT ON COLUMN services.service_id IS '服务唯一标识（业务主键，通常来自配置或注册接口）。';
COMMENT ON COLUMN services.name IS '服务显示名称。';
COMMENT ON COLUMN services.description IS '服务描述。';
COMMENT ON COLUMN services.version IS '服务版本号。';
COMMENT ON COLUMN services.service_type IS '服务类型（如 custom / conversational / text2image 等，取值由业务约定）。';
COMMENT ON COLUMN services.connector_type IS '连接器类型（http/openai/dify/langgraph 等）。';
COMMENT ON COLUMN services.connector_config IS '连接器配置（base_url、headers、模型名等）。';
COMMENT ON COLUMN services.supported_modes IS '支持的调用模式（sync/stream/async）。';
COMMENT ON COLUMN services.accepted_content_types IS '支持的输入内容类型（text/image/audio 等）。';
COMMENT ON COLUMN services.output_content_types IS '输出内容类型（text/image/audio 等）。';
COMMENT ON COLUMN services.input_schema IS '输入 JSON Schema（可选，用于校验/生成 UI）。';
COMMENT ON COLUMN services.output_schema IS '输出 JSON Schema（可选）。';
COMMENT ON COLUMN services.session_enabled IS '是否启用会话能力（session_id）。';
COMMENT ON COLUMN services.session_adapter IS '会话适配器名称（可选）。';
COMMENT ON COLUMN services.timeout IS '请求超时（秒）。';
COMMENT ON COLUMN services.max_retries IS '最大重试次数。';
COMMENT ON COLUMN services.retry_delay IS '重试间隔（秒，支持小数）。';
COMMENT ON COLUMN services.circuit_breaker_enabled IS '是否启用熔断。';
COMMENT ON COLUMN services.failure_threshold IS '触发熔断的失败阈值（次数）。';
COMMENT ON COLUMN services.recovery_timeout IS '熔断后恢复等待时间（秒）。';
COMMENT ON COLUMN services.rate_limit IS '服务级限流配置（兼容字段）。';
COMMENT ON COLUMN services.concurrency_limit IS '并发限制（可选）。';
COMMENT ON COLUMN services.service_config IS '服务综合配置（新版字段：限流/鉴权/缓存/优先级等）。';
COMMENT ON COLUMN services.async_config IS '异步执行配置（回调、队列等）。';
COMMENT ON COLUMN services.status IS '服务状态（active/inactive/disabled 等）。';
COMMENT ON COLUMN services.tags IS '标签数组，用于筛选/分组。';
COMMENT ON COLUMN services.metadata IS '扩展元数据（如 adapter_type、分类等）。';
COMMENT ON COLUMN services.created_at IS '创建时间。';
COMMENT ON COLUMN services.updated_at IS '更新时间（由触发器自动维护）。';

-- ============================================================
-- 2. API Key 表
-- ============================================================
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    key_hash VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255),
    description TEXT,
    tenant_id VARCHAR(255),
    user_id VARCHAR(255),
    roles VARCHAR(50)[] NOT NULL DEFAULT ARRAY['user']::VARCHAR(50)[],
    permissions VARCHAR(100)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(100)[],
    tier VARCHAR(50) NOT NULL DEFAULT 'normal',
    rate_limit JSONB,
    allowed_services VARCHAR(255)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(255)[],
    expires_at TIMESTAMPTZ,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    use_count BIGINT NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE api_keys IS 'API Key 管理表：用于请求鉴权、权限控制、配额/限流策略等。';
COMMENT ON COLUMN api_keys.id IS '自增主键。';
COMMENT ON COLUMN api_keys.key_hash IS 'API Key 哈希（仅存哈希，不存明文）。';
COMMENT ON COLUMN api_keys.name IS 'Key 名称（便于识别）。';
COMMENT ON COLUMN api_keys.description IS 'Key 描述。';
COMMENT ON COLUMN api_keys.tenant_id IS '租户 ID（可选，空表示不限定租户）。';
COMMENT ON COLUMN api_keys.user_id IS '用户 ID（可选）。';
COMMENT ON COLUMN api_keys.roles IS '角色列表（用于 RBAC）。';
COMMENT ON COLUMN api_keys.permissions IS '权限列表（可选，细粒度权限）。';
COMMENT ON COLUMN api_keys.tier IS '用户层级（anonymous/normal/premium/enterprise/admin 等）。';
COMMENT ON COLUMN api_keys.rate_limit IS 'Key 级限流覆盖配置（可选）。';
COMMENT ON COLUMN api_keys.allowed_services IS '允许访问的服务列表（可选）。';
COMMENT ON COLUMN api_keys.expires_at IS '过期时间（可选）。';
COMMENT ON COLUMN api_keys.enabled IS '是否启用。';
COMMENT ON COLUMN api_keys.last_used_at IS '最后使用时间。';
COMMENT ON COLUMN api_keys.use_count IS '使用次数累计。';
COMMENT ON COLUMN api_keys.created_at IS '创建时间。';
COMMENT ON COLUMN api_keys.updated_at IS '更新时间（由触发器自动维护）。';

-- ============================================================
-- 3. 用户表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL UNIQUE,
    username VARCHAR(255),
    email VARCHAR(255),
    display_name VARCHAR(255),
    tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',
    tier VARCHAR(50) NOT NULL DEFAULT 'normal',
    roles VARCHAR(50)[] NOT NULL DEFAULT ARRAY['user']::VARCHAR(50)[],
    permissions VARCHAR(100)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(100)[],
    quota_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ
);

COMMENT ON TABLE users IS '用户表（预留）：用于持久化用户画像、权限、配额等（当前项目主要从 JWT/API Key 解析）。';
COMMENT ON COLUMN users.id IS '自增主键。';
COMMENT ON COLUMN users.user_id IS '用户唯一标识（业务主键，通常来自 JWT sub / 外部系统）。';
COMMENT ON COLUMN users.username IS '用户名（可选）。';
COMMENT ON COLUMN users.email IS '邮箱（可选）。';
COMMENT ON COLUMN users.display_name IS '显示名（可选）。';
COMMENT ON COLUMN users.tenant_id IS '所属租户 ID。';
COMMENT ON COLUMN users.tier IS '用户层级（anonymous/normal/premium/enterprise/admin 等）。';
COMMENT ON COLUMN users.roles IS '角色列表。';
COMMENT ON COLUMN users.permissions IS '权限列表（可选）。';
COMMENT ON COLUMN users.quota_config IS '配额配置（如 token/请求数上限等，可选）。';
COMMENT ON COLUMN users.status IS '用户状态（active/disabled 等）。';
COMMENT ON COLUMN users.metadata IS '扩展元数据。';
COMMENT ON COLUMN users.created_at IS '创建时间。';
COMMENT ON COLUMN users.updated_at IS '更新时间（由触发器自动维护）。';
COMMENT ON COLUMN users.last_active_at IS '最后活跃时间。';

-- ============================================================
-- 4. 租户表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(255) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    tier VARCHAR(50) NOT NULL DEFAULT 'normal',
    quota_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    rate_limit JSONB,
    allowed_services VARCHAR(255)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(255)[],
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tenants IS '租户表：用于多租户隔离、配额与限流策略。';
COMMENT ON COLUMN tenants.id IS '自增主键。';
COMMENT ON COLUMN tenants.tenant_id IS '租户唯一标识（业务主键）。';
COMMENT ON COLUMN tenants.name IS '租户名称。';
COMMENT ON COLUMN tenants.description IS '租户描述。';
COMMENT ON COLUMN tenants.tier IS '租户层级（用于策略分层）。';
COMMENT ON COLUMN tenants.quota_config IS '租户配额配置（可选）。';
COMMENT ON COLUMN tenants.rate_limit IS '租户级限流覆盖配置（可选）。';
COMMENT ON COLUMN tenants.allowed_services IS '允许使用的服务列表（可选）。';
COMMENT ON COLUMN tenants.status IS '租户状态（active/disabled 等）。';
COMMENT ON COLUMN tenants.metadata IS '扩展元数据。';
COMMENT ON COLUMN tenants.created_at IS '创建时间。';
COMMENT ON COLUMN tenants.updated_at IS '更新时间（由触发器自动维护）。';

-- 预置常用租户（可按需调整）
INSERT INTO tenants (tenant_id, name, description, tier, status, metadata)
VALUES
    ('default', 'Default', '系统默认租户', 'normal', 'active', '{}'::jsonb),
    ('public', 'Public', '匿名/公共租户', 'normal', 'active', '{}'::jsonb),
    ('local', 'Local', '本地开发租户', 'normal', 'active', '{}'::jsonb)
ON CONFLICT (tenant_id) DO NOTHING;

-- ============================================================
-- 5. 会话表
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    service_id VARCHAR(255),
    user_id VARCHAR(255),
    tenant_id VARCHAR(255),
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    history JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE sessions IS '会话表：用于保存 session 状态、历史消息与扩展元数据。';
COMMENT ON COLUMN sessions.session_id IS '会话 ID（通常为 UUID 字符串）。';
COMMENT ON COLUMN sessions.service_id IS '关联服务 ID（可选；会话可能独立于服务创建）。';
COMMENT ON COLUMN sessions.user_id IS '用户 ID（可选）。';
COMMENT ON COLUMN sessions.tenant_id IS '租户 ID（可选）。';
COMMENT ON COLUMN sessions.state IS '会话状态（中间变量、上下文等）。';
COMMENT ON COLUMN sessions.history IS '历史消息列表（JSON 数组）。';
COMMENT ON COLUMN sessions.metadata IS '扩展元数据。';
COMMENT ON COLUMN sessions.config IS '会话级配置（如参数覆盖等）。';
COMMENT ON COLUMN sessions.status IS '会话状态（active/expired/closed 等）。';
COMMENT ON COLUMN sessions.expires_at IS '过期时间（可选）。';
COMMENT ON COLUMN sessions.created_at IS '创建时间。';
COMMENT ON COLUMN sessions.updated_at IS '更新时间（由触发器自动维护）。';

-- ============================================================
-- 6. 异步任务表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS tasks (
    task_id VARCHAR(255) PRIMARY KEY,
    request_id VARCHAR(255) NOT NULL,
    service_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255),
    tenant_id VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    progress NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    request_data JSONB,
    result JSONB,
    error TEXT,
    callback_url TEXT,
    callback_sent BOOLEAN NOT NULL DEFAULT FALSE,
    priority INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

COMMENT ON TABLE tasks IS '异步任务表（预留）：用于持久化任务执行状态、回调与结果。';
COMMENT ON COLUMN tasks.task_id IS '任务 ID（通常为 UUID 字符串）。';
COMMENT ON COLUMN tasks.request_id IS '请求 ID（来自统一请求模型）。';
COMMENT ON COLUMN tasks.service_id IS '服务 ID。';
COMMENT ON COLUMN tasks.user_id IS '用户 ID（可选）。';
COMMENT ON COLUMN tasks.tenant_id IS '租户 ID（可选）。';
COMMENT ON COLUMN tasks.status IS '任务状态（pending/processing/completed/failed/cancelled）。';
COMMENT ON COLUMN tasks.progress IS '任务进度（0-100）。';
COMMENT ON COLUMN tasks.request_data IS '请求数据快照（JSON）。';
COMMENT ON COLUMN tasks.result IS '结果数据（JSON）。';
COMMENT ON COLUMN tasks.error IS '错误信息。';
COMMENT ON COLUMN tasks.callback_url IS '回调地址（可选）。';
COMMENT ON COLUMN tasks.callback_sent IS '是否已发送回调。';
COMMENT ON COLUMN tasks.priority IS '任务优先级（数值越大优先级越高/或按业务约定）。';
COMMENT ON COLUMN tasks.retry_count IS '已重试次数。';
COMMENT ON COLUMN tasks.max_retries IS '最大重试次数。';
COMMENT ON COLUMN tasks.metadata IS '扩展元数据。';
COMMENT ON COLUMN tasks.created_at IS '创建时间。';
COMMENT ON COLUMN tasks.updated_at IS '更新时间（由触发器自动维护）。';
COMMENT ON COLUMN tasks.started_at IS '开始执行时间。';
COMMENT ON COLUMN tasks.completed_at IS '完成时间。';

-- ============================================================
-- 6b. Knowledge Base (KBMS)
-- ============================================================
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    tenant_id VARCHAR(255) NOT NULL DEFAULT '',
    visibility VARCHAR(50) NOT NULL DEFAULT 'private', -- private|tenant|public
    embedding_provider VARCHAR(50) NOT NULL DEFAULT 'openai',
    embedding_model VARCHAR(100) NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimension INTEGER NOT NULL DEFAULT 1536 CHECK (embedding_dimension > 0),
    embedding_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    index_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    collection_name VARCHAR(255),
    created_by VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS documents (
    document_id VARCHAR(255) PRIMARY KEY,
    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    source_type VARCHAR(50) NOT NULL DEFAULT 'upload', -- upload|text|url
    source_uri TEXT,
    mime_type VARCHAR(100),
    size_bytes BIGINT CHECK (size_bytes IS NULL OR size_bytes >= 0),
    status VARCHAR(50) NOT NULL DEFAULT 'uploaded', -- uploaded|parsing|segmenting|embedding|completed|failed
    progress NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    error TEXT,
    content TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS segments (
    segment_id VARCHAR(255) PRIMARY KEY,
    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    document_id VARCHAR(255) NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0 CHECK (token_count >= 0),
    vector_id VARCHAR(255),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Image segment fields (from 010_image_segments)
    content_type VARCHAR(50) NOT NULL DEFAULT 'text',  -- text | image
    image_url TEXT,
    image_attachment_id VARCHAR(100),
    image_filename VARCHAR(255),
    image_media_type VARCHAR(100),
    image_file_size INTEGER,
    -- Multimodal chunk fields (from 014_multimodal_chunks)
    has_images BOOLEAN NOT NULL DEFAULT FALSE,
    image_count INTEGER NOT NULL DEFAULT 0 CHECK (image_count >= 0),
    vlm_description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(document_id, position)
);

-- Image-Chunk Association Table (014_multimodal_chunks, 015_segment_images_fk)
-- Links image segments to text segments for multimodal retrieval
CREATE TABLE IF NOT EXISTS segment_images (
    id BIGSERIAL PRIMARY KEY,
    segment_id VARCHAR(255) NOT NULL REFERENCES segments(segment_id) ON DELETE CASCADE,
    image_segment_id VARCHAR(255) NOT NULL REFERENCES segments(segment_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    proximity_score FLOAT NOT NULL DEFAULT 1.0 CHECK (proximity_score >= 0 AND proximity_score <= 1),
    char_offset INTEGER DEFAULT 0,
    page_number INTEGER DEFAULT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(segment_id, image_segment_id)
);

CREATE INDEX IF NOT EXISTS idx_segment_images_segment ON segment_images(segment_id);
CREATE INDEX IF NOT EXISTS idx_segment_images_image ON segment_images(image_segment_id);
CREATE INDEX IF NOT EXISTS idx_segments_content_type ON segments(content_type);
CREATE INDEX IF NOT EXISTS idx_segments_has_images ON segments(has_images) WHERE has_images = TRUE;

CREATE TABLE IF NOT EXISTS dataset_permissions (
    id BIGSERIAL PRIMARY KEY,
    dataset_id VARCHAR(255) NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
    subject_type VARCHAR(50) NOT NULL, -- user|role
    subject_id VARCHAR(255) NOT NULL,
    permission VARCHAR(50) NOT NULL DEFAULT 'viewer', -- owner|editor|viewer
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(dataset_id, subject_type, subject_id)
);

-- ============================================================
-- 7. 鉴权配置表
-- ============================================================
CREATE TABLE IF NOT EXISTS auth_config (
    id SERIAL PRIMARY KEY,
    config_type VARCHAR(50) NOT NULL UNIQUE,
    config JSONB NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE auth_config IS '鉴权配置表：持久化 JWT/API Key 等鉴权开关与配置（可选）。';
COMMENT ON COLUMN auth_config.id IS '自增主键。';
COMMENT ON COLUMN auth_config.config_type IS '配置类型（如 jwt / api_key / rbac 等）。';
COMMENT ON COLUMN auth_config.config IS '配置内容（JSON）。';
COMMENT ON COLUMN auth_config.enabled IS '是否启用该配置项。';
COMMENT ON COLUMN auth_config.created_at IS '创建时间。';
COMMENT ON COLUMN auth_config.updated_at IS '更新时间（由触发器自动维护）。';

-- ============================================================
-- 8. RBAC 角色权限表（预置）
-- ============================================================
CREATE TABLE IF NOT EXISTS rbac_roles (
    id SERIAL PRIMARY KEY,
    role_name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    permissions VARCHAR(100)[] NOT NULL DEFAULT ARRAY[]::VARCHAR(100)[],
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE rbac_roles IS 'RBAC 角色表：定义角色与权限列表（当前系统也支持从配置文件加载）。';
COMMENT ON COLUMN rbac_roles.id IS '自增主键。';
COMMENT ON COLUMN rbac_roles.role_name IS '角色名（唯一）。';
COMMENT ON COLUMN rbac_roles.description IS '角色描述。';
COMMENT ON COLUMN rbac_roles.permissions IS '权限列表（如 service:invoke / admin:*）。';
COMMENT ON COLUMN rbac_roles.is_system IS '是否系统内置角色。';
COMMENT ON COLUMN rbac_roles.created_at IS '创建时间。';
COMMENT ON COLUMN rbac_roles.updated_at IS '更新时间（由触发器自动维护）。';

-- 插入默认角色
INSERT INTO rbac_roles (role_name, permissions, is_system) VALUES
    ('guest', ARRAY['service:invoke:public']::VARCHAR(100)[], TRUE),
    ('user', ARRAY['service:invoke', 'task:read']::VARCHAR(100)[], TRUE),
    ('developer', ARRAY['service:invoke', 'task:read', 'task:write', 'service:manage']::VARCHAR(100)[], TRUE),
    ('admin', ARRAY['admin:*']::VARCHAR(100)[], TRUE)
ON CONFLICT (role_name) DO NOTHING;

-- ============================================================
-- 9. 限流规则表
-- ============================================================
-- 兼容迁移：避免使用保留关键字 window 作为列名
DO $$
BEGIN
    IF to_regclass('public.rate_limit_config') IS NOT NULL
       AND EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'rate_limit_config'
              AND column_name = 'window'
        )
    THEN
        EXECUTE 'ALTER TABLE rate_limit_config RENAME COLUMN "window" TO window_seconds';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS rate_limit_config (
    id SERIAL PRIMARY KEY,
    scope VARCHAR(50) NOT NULL,
    scope_id VARCHAR(255) NOT NULL DEFAULT '',
    requests INTEGER NOT NULL CHECK (requests > 0),
    window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
    burst INTEGER NOT NULL DEFAULT 0 CHECK (burst >= 0),
    strategy VARCHAR(50) NOT NULL DEFAULT 'sliding_window',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(scope, scope_id)
);

COMMENT ON TABLE rate_limit_config IS '限流规则表：支持 global/ip/user/tenant/service 等多范围规则。';
COMMENT ON COLUMN rate_limit_config.id IS '自增主键。';
COMMENT ON COLUMN rate_limit_config.scope IS '限流范围（global/ip/user/tenant/service）。';
COMMENT ON COLUMN rate_limit_config.scope_id IS '范围 ID（如 service_id、tenant_id；global 可为空字符串）。';
COMMENT ON COLUMN rate_limit_config.requests IS '允许的请求数。';
COMMENT ON COLUMN rate_limit_config.window_seconds IS '时间窗口（秒）。';
COMMENT ON COLUMN rate_limit_config.burst IS '突发容量（可选）。';
COMMENT ON COLUMN rate_limit_config.strategy IS '限流算法（sliding_window/fixed_window/token_bucket）。';
COMMENT ON COLUMN rate_limit_config.enabled IS '是否启用。';
COMMENT ON COLUMN rate_limit_config.priority IS '规则优先级（用于冲突时择优）。';
COMMENT ON COLUMN rate_limit_config.created_at IS '创建时间。';
COMMENT ON COLUMN rate_limit_config.updated_at IS '更新时间（由触发器自动维护）。';

-- 插入默认限流配置
INSERT INTO rate_limit_config (scope, scope_id, requests, window_seconds, burst, priority) VALUES
    ('global', '', 1000, 60, 100, 0)
ON CONFLICT (scope, scope_id) DO NOTHING;

-- ============================================================
-- 10. 语义缓存表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS semantic_cache (
    id SERIAL PRIMARY KEY,
    service_id VARCHAR(255) NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    input_embedding BYTEA,
    input_text TEXT,
    output_text TEXT NOT NULL,
    output_data JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    hit_count INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
    last_hit_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(service_id, input_hash)
);

COMMENT ON TABLE semantic_cache IS '语义缓存表（预留）：用于基于输入特征的结果缓存与命中统计。';
COMMENT ON COLUMN semantic_cache.id IS '自增主键。';
COMMENT ON COLUMN semantic_cache.service_id IS '服务 ID。';
COMMENT ON COLUMN semantic_cache.input_hash IS '输入哈希（用于快速匹配）。';
COMMENT ON COLUMN semantic_cache.input_embedding IS '输入向量（可选）。';
COMMENT ON COLUMN semantic_cache.input_text IS '输入文本（可选，便于排查）。';
COMMENT ON COLUMN semantic_cache.output_text IS '输出文本（兼容字段）。';
COMMENT ON COLUMN semantic_cache.output_data IS '输出结构化数据（JSON，可选）。';
COMMENT ON COLUMN semantic_cache.metadata IS '扩展元数据。';
COMMENT ON COLUMN semantic_cache.hit_count IS '命中次数。';
COMMENT ON COLUMN semantic_cache.last_hit_at IS '最后命中时间。';
COMMENT ON COLUMN semantic_cache.expires_at IS '过期时间（可选）。';
COMMENT ON COLUMN semantic_cache.created_at IS '创建时间。';

-- ============================================================
-- 11. 审计日志表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    user_id VARCHAR(255),
    tenant_id VARCHAR(255),
    ip_address VARCHAR(45),
    user_agent TEXT,
    resource_type VARCHAR(100),
    resource_id VARCHAR(255),
    action VARCHAR(50) NOT NULL,
    request_summary JSONB,
    response_summary JSONB,
    status VARCHAR(50) NOT NULL,
    error_message TEXT,
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE audit_logs IS '审计日志表（预留）：记录关键操作与请求/响应摘要，便于合规与排障。';
COMMENT ON COLUMN audit_logs.id IS '自增主键。';
COMMENT ON COLUMN audit_logs.event_type IS '事件类型（如 invoke/service/register 等）。';
COMMENT ON COLUMN audit_logs.user_id IS '用户 ID（可选）。';
COMMENT ON COLUMN audit_logs.tenant_id IS '租户 ID（可选）。';
COMMENT ON COLUMN audit_logs.ip_address IS '客户端 IP（IPv4/IPv6 字符串）。';
COMMENT ON COLUMN audit_logs.user_agent IS 'User-Agent。';
COMMENT ON COLUMN audit_logs.resource_type IS '资源类型（如 service/session/task）。';
COMMENT ON COLUMN audit_logs.resource_id IS '资源 ID（如 service_id）。';
COMMENT ON COLUMN audit_logs.action IS '动作（create/update/delete/invoke 等）。';
COMMENT ON COLUMN audit_logs.request_summary IS '请求摘要（JSON）。';
COMMENT ON COLUMN audit_logs.response_summary IS '响应摘要（JSON）。';
COMMENT ON COLUMN audit_logs.status IS '处理状态（success/failed 等）。';
COMMENT ON COLUMN audit_logs.error_message IS '错误信息（可选）。';
COMMENT ON COLUMN audit_logs.duration_ms IS '耗时（毫秒，可选）。';
COMMENT ON COLUMN audit_logs.created_at IS '创建时间。';

-- ============================================================
-- 12. 服务健康记录表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS service_health_records (
    id BIGSERIAL PRIMARY KEY,
    service_id VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL,
    response_time_ms INTEGER CHECK (response_time_ms IS NULL OR response_time_ms >= 0),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE service_health_records IS '服务健康记录表（预留）：记录健康检查结果与响应时间。';
COMMENT ON COLUMN service_health_records.id IS '自增主键。';
COMMENT ON COLUMN service_health_records.service_id IS '服务 ID。';
COMMENT ON COLUMN service_health_records.status IS '健康状态（healthy/unhealthy/timeout 等）。';
COMMENT ON COLUMN service_health_records.response_time_ms IS '响应时间（毫秒，可选）。';
COMMENT ON COLUMN service_health_records.details IS '检查详情（JSON）。';
COMMENT ON COLUMN service_health_records.error_message IS '错误信息（可选）。';
COMMENT ON COLUMN service_health_records.checked_at IS '检查时间。';

-- ============================================================
-- 13. 使用统计表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS usage_statistics (
    id BIGSERIAL PRIMARY KEY,
    dimension VARCHAR(50) NOT NULL,
    dimension_id VARCHAR(255) NOT NULL,
    period_type VARCHAR(20) NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    request_count BIGINT NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    success_count BIGINT NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    error_count BIGINT NOT NULL DEFAULT 0 CHECK (error_count >= 0),
    input_tokens BIGINT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens BIGINT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    avg_response_time_ms INTEGER CHECK (avg_response_time_ms IS NULL OR avg_response_time_ms >= 0),
    max_response_time_ms INTEGER CHECK (max_response_time_ms IS NULL OR max_response_time_ms >= 0),
    min_response_time_ms INTEGER CHECK (min_response_time_ms IS NULL OR min_response_time_ms >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(dimension, dimension_id, period_type, period_start)
);

COMMENT ON TABLE usage_statistics IS '使用统计表（预留）：按维度聚合请求量、token 量与延迟等指标。';
COMMENT ON COLUMN usage_statistics.id IS '自增主键。';
COMMENT ON COLUMN usage_statistics.dimension IS '统计维度（global/tenant/user/service 等）。';
COMMENT ON COLUMN usage_statistics.dimension_id IS '维度 ID（如 tenant_id、service_id）。';
COMMENT ON COLUMN usage_statistics.period_type IS '周期类型（minute/hour/day/month）。';
COMMENT ON COLUMN usage_statistics.period_start IS '周期起始时间。';
COMMENT ON COLUMN usage_statistics.request_count IS '请求总数。';
COMMENT ON COLUMN usage_statistics.success_count IS '成功请求数。';
COMMENT ON COLUMN usage_statistics.error_count IS '错误请求数。';
COMMENT ON COLUMN usage_statistics.input_tokens IS '输入 token 数。';
COMMENT ON COLUMN usage_statistics.output_tokens IS '输出 token 数。';
COMMENT ON COLUMN usage_statistics.avg_response_time_ms IS '平均响应时间（毫秒，可选）。';
COMMENT ON COLUMN usage_statistics.max_response_time_ms IS '最大响应时间（毫秒，可选）。';
COMMENT ON COLUMN usage_statistics.min_response_time_ms IS '最小响应时间（毫秒，可选）。';
COMMENT ON COLUMN usage_statistics.created_at IS '创建时间。';
COMMENT ON COLUMN usage_statistics.updated_at IS '更新时间（由触发器自动维护）。';

-- ============================================================
-- 14. LangGraph Thread 映射表（预留）
-- ============================================================
CREATE TABLE IF NOT EXISTS langgraph_threads (
    id SERIAL PRIMARY KEY,
    thread_id VARCHAR(255) NOT NULL UNIQUE,
    user_id VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL DEFAULT '',
    assistant_id VARCHAR(255),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    is_anonymous BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ
);

COMMENT ON TABLE langgraph_threads IS 'LangGraph Thread 映射表（预留）：用于把网关用户/会话映射到 LangGraph thread。';
COMMENT ON COLUMN langgraph_threads.id IS '自增主键。';
COMMENT ON COLUMN langgraph_threads.thread_id IS 'LangGraph Thread ID。';
COMMENT ON COLUMN langgraph_threads.user_id IS '用户 ID。';
COMMENT ON COLUMN langgraph_threads.tenant_id IS '租户 ID。';
COMMENT ON COLUMN langgraph_threads.assistant_id IS 'Assistant/Graph ID（可选）。';
COMMENT ON COLUMN langgraph_threads.metadata IS '扩展元数据（可包含 session_id、标签等）。';
COMMENT ON COLUMN langgraph_threads.status IS '状态（active/expired/disabled 等）。';
COMMENT ON COLUMN langgraph_threads.is_anonymous IS '是否匿名 Thread。';
COMMENT ON COLUMN langgraph_threads.expires_at IS '过期时间（可选）。';
COMMENT ON COLUMN langgraph_threads.created_at IS '创建时间。';
COMMENT ON COLUMN langgraph_threads.updated_at IS '更新时间（由触发器自动维护）。';
COMMENT ON COLUMN langgraph_threads.last_accessed_at IS '最后访问时间。';

-- ============================================================
-- 索引
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_services_service_type ON services(service_type);
CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);
CREATE INDEX IF NOT EXISTS idx_services_tags ON services USING GIN(tags);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id ON api_keys(tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled);
CREATE INDEX IF NOT EXISTS idx_api_keys_expires_at ON api_keys(expires_at);

CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE INDEX IF NOT EXISTS idx_sessions_service_id ON sessions(service_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_id ON sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE INDEX IF NOT EXISTS idx_tasks_request_id ON tasks(request_id);
CREATE INDEX IF NOT EXISTS idx_tasks_service_id ON tasks(service_id);
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant_id ON tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_datasets_tenant_id ON datasets(tenant_id);
CREATE INDEX IF NOT EXISTS idx_datasets_visibility ON datasets(visibility);

CREATE INDEX IF NOT EXISTS idx_documents_dataset_id ON documents(dataset_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_segments_dataset_id ON segments(dataset_id);
CREATE INDEX IF NOT EXISTS idx_segments_document_id ON segments(document_id);
CREATE INDEX IF NOT EXISTS idx_segments_vector_id ON segments(vector_id);

CREATE INDEX IF NOT EXISTS idx_dataset_permissions_dataset_id ON dataset_permissions(dataset_id);
CREATE INDEX IF NOT EXISTS idx_dataset_permissions_subject ON dataset_permissions(subject_type, subject_id);

CREATE INDEX IF NOT EXISTS idx_semantic_cache_service_id ON semantic_cache(service_id);
CREATE INDEX IF NOT EXISTS idx_semantic_cache_expires_at ON semantic_cache(expires_at);

CREATE INDEX IF NOT EXISTS idx_audit_logs_event_type ON audit_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_id ON audit_logs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_health_records_service_id ON service_health_records(service_id);
CREATE INDEX IF NOT EXISTS idx_health_records_checked_at ON service_health_records(checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_usage_statistics_period_start ON usage_statistics(period_start DESC);

CREATE INDEX IF NOT EXISTS idx_langgraph_threads_user_id ON langgraph_threads(user_id);
CREATE INDEX IF NOT EXISTS idx_langgraph_threads_tenant_id ON langgraph_threads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_langgraph_threads_status ON langgraph_threads(status);

-- ============================================================
-- 触发器：自动更新 updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_services_updated_at ON services;
CREATE TRIGGER update_services_updated_at BEFORE UPDATE ON services
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_api_keys_updated_at ON api_keys;
CREATE TRIGGER update_api_keys_updated_at BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_users_updated_at ON users;
CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_tenants_updated_at ON tenants;
CREATE TRIGGER update_tenants_updated_at BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_sessions_updated_at ON sessions;
CREATE TRIGGER update_sessions_updated_at BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_tasks_updated_at ON tasks;
CREATE TRIGGER update_tasks_updated_at BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_datasets_updated_at ON datasets;
CREATE TRIGGER update_datasets_updated_at BEFORE UPDATE ON datasets
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_documents_updated_at ON documents;
CREATE TRIGGER update_documents_updated_at BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_segments_updated_at ON segments;
CREATE TRIGGER update_segments_updated_at BEFORE UPDATE ON segments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_dataset_permissions_updated_at ON dataset_permissions;
CREATE TRIGGER update_dataset_permissions_updated_at BEFORE UPDATE ON dataset_permissions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_auth_config_updated_at ON auth_config;
CREATE TRIGGER update_auth_config_updated_at BEFORE UPDATE ON auth_config
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_rbac_roles_updated_at ON rbac_roles;
CREATE TRIGGER update_rbac_roles_updated_at BEFORE UPDATE ON rbac_roles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_rate_limit_config_updated_at ON rate_limit_config;
CREATE TRIGGER update_rate_limit_config_updated_at BEFORE UPDATE ON rate_limit_config
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_usage_statistics_updated_at ON usage_statistics;
CREATE TRIGGER update_usage_statistics_updated_at BEFORE UPDATE ON usage_statistics
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_langgraph_threads_updated_at ON langgraph_threads;
CREATE TRIGGER update_langgraph_threads_updated_at BEFORE UPDATE ON langgraph_threads
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
