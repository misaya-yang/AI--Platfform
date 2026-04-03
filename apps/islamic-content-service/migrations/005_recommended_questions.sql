-- 005: Recommended Questions — AI-generated personalized follow-up questions
-- based on user conversation history.

CREATE TABLE IF NOT EXISTS islamic_content.recommended_questions (
    question_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            VARCHAR(64)  NOT NULL,
    user_id              VARCHAR(64)  NOT NULL,
    session_id           VARCHAR(255),
    date_trigger         DATE         NOT NULL,
    question_text        TEXT         NOT NULL,
    is_regen             BOOLEAN      NOT NULL DEFAULT false,
    source_message_index INT,
    source_topic         VARCHAR(200),
    status               VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at           TIMESTAMPTZ  DEFAULT NOW(),
    updated_at           TIMESTAMPTZ  DEFAULT NOW()
);

-- Primary query: today's active recommendations for a user
CREATE INDEX IF NOT EXISTS idx_rq_user_date
    ON islamic_content.recommended_questions (tenant_id, user_id, date_trigger)
    WHERE status = 'active';

-- Lookup by source session
CREATE INDEX IF NOT EXISTS idx_rq_session
    ON islamic_content.recommended_questions (session_id);

-- Cleanup / expiry scans
CREATE INDEX IF NOT EXISTS idx_rq_created
    ON islamic_content.recommended_questions (created_at);
