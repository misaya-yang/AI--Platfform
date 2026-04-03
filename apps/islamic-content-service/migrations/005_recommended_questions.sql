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

-- L-1: History pagination query index
CREATE INDEX IF NOT EXISTS idx_rq_user_history
    ON islamic_content.recommended_questions (tenant_id, user_id, created_at DESC);

-- M-3: Auto-update updated_at on row modification
CREATE OR REPLACE FUNCTION islamic_content.update_rq_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'trg_rq_updated_at'
  ) THEN
    CREATE TRIGGER trg_rq_updated_at
      BEFORE UPDATE ON islamic_content.recommended_questions
      FOR EACH ROW EXECUTE FUNCTION islamic_content.update_rq_updated_at();
  END IF;
END $$;
