-- 058_image_model_override.sql
--
-- Add model type metadata so chat and image generation can be configured
-- independently from the same Gateway provider/model control plane.

BEGIN;

ALTER TABLE llm_models
    ADD COLUMN IF NOT EXISTS model_type VARCHAR(32) NOT NULL DEFAULT 'llm';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'llm_models_model_type_check'
    ) THEN
        ALTER TABLE llm_models
            ADD CONSTRAINT llm_models_model_type_check
            CHECK (model_type IN ('llm', 'image', 'multimodal', 'embedding', 'reranker'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_llm_models_tenant_provider_type
    ON llm_models(tenant_id, provider_id, model_type, is_enabled);

UPDATE llm_models
SET model_type = 'image',
    supports_tools = false,
    updated_at = NOW()
WHERE lower(model_id) LIKE '%image%'
   OR lower(model_id) LIKE '%imagen%'
   OR lower(model_id) LIKE '%wanx%'
   OR lower(model_id) LIKE '%seedream%';

DO $$
DECLARE
    has_provider_in_pk BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_attribute a
          ON a.attrelid = c.conrelid
         AND a.attnum = ANY(c.conkey)
        WHERE c.conrelid = 'llm_models'::regclass
          AND c.contype = 'p'
          AND a.attname = 'provider_id'
    )
    INTO has_provider_in_pk;

    IF has_provider_in_pk THEN
        INSERT INTO llm_models (
            model_id, tenant_id, provider_id, display_name, model_type,
            context_window, max_output_tokens, supports_vision, supports_tools,
            input_price_per_1k, output_price_per_1k, access_level, is_enabled, sort_order
        )
        VALUES
            (
                'gemini-3.1-flash-image-preview', 'default', 'google',
                'Gemini 3.1 Flash Image', 'image',
                1000000, 8192, true, false,
                0, 0, 'public', true, 100
            ),
            (
                'qwen-image-2.0', 'default', 'dashscope',
                'Qwen Image 2.0', 'image',
                1, 1, false, false,
                0, 0, 'public', true, 100
            ),
            (
                'wanx-v1', 'default', 'dashscope',
                'Wanx v1', 'image',
                1, 1, false, false,
                0, 0, 'public', true, 101
            )
        ON CONFLICT (tenant_id, provider_id, model_id)
        DO UPDATE SET
            display_name = EXCLUDED.display_name,
            model_type = EXCLUDED.model_type,
            supports_vision = EXCLUDED.supports_vision,
            supports_tools = EXCLUDED.supports_tools,
            is_enabled = EXCLUDED.is_enabled,
            sort_order = EXCLUDED.sort_order,
            updated_at = NOW();
    ELSE
        INSERT INTO llm_models (
            model_id, tenant_id, provider_id, display_name, model_type,
            context_window, max_output_tokens, supports_vision, supports_tools,
            input_price_per_1k, output_price_per_1k, access_level, is_enabled, sort_order
        )
        VALUES
            (
                'gemini-3.1-flash-image-preview', 'default', 'google',
                'Gemini 3.1 Flash Image', 'image',
                1000000, 8192, true, false,
                0, 0, 'public', true, 100
            ),
            (
                'qwen-image-2.0', 'default', 'dashscope',
                'Qwen Image 2.0', 'image',
                1, 1, false, false,
                0, 0, 'public', true, 100
            ),
            (
                'wanx-v1', 'default', 'dashscope',
                'Wanx v1', 'image',
                1, 1, false, false,
                0, 0, 'public', true, 101
            )
        ON CONFLICT (tenant_id, model_id)
        DO UPDATE SET
            provider_id = EXCLUDED.provider_id,
            display_name = EXCLUDED.display_name,
            model_type = EXCLUDED.model_type,
            supports_vision = EXCLUDED.supports_vision,
            supports_tools = EXCLUDED.supports_tools,
            is_enabled = EXCLUDED.is_enabled,
            sort_order = EXCLUDED.sort_order,
            updated_at = NOW();
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS assistant_service_configs (
    tenant_id VARCHAR(100) PRIMARY KEY,
    image_model_override JSONB NOT NULL DEFAULT '{"enabled": false}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMIT;
