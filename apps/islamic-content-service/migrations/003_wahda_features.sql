-- 003_wahda_features.sql
-- Sheikh Wahda recommendation system: religious events, question pools, feedback

-- Religious events with date-based question packages
CREATE TABLE IF NOT EXISTS islamic_content.religious_events (
    event_id        SERIAL PRIMARY KEY,
    event_name      VARCHAR(100) NOT NULL,
    islamic_date    VARCHAR(50),
    gregorian_start DATE NOT NULL,
    gregorian_end   DATE,
    recurrence      VARCHAR(20) DEFAULT 'yearly',   -- yearly | monthly | weekly | daily
    weekday         SMALLINT,                        -- 0=Mon..6=Sun (5=Fri for Jumu'ah)
    lunar_days      SMALLINT[],                      -- {13,14,15} for Ayyam al-Beed
    questions       JSONB NOT NULL DEFAULT '[]',
    description     TEXT,
    priority        SMALLINT DEFAULT 0,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_re_dates
    ON islamic_content.religious_events (gregorian_start, gregorian_end)
    WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_re_recurrence
    ON islamic_content.religious_events (recurrence)
    WHERE is_active;

-- Question pool for typeahead and daily fallback
CREATE TABLE IF NOT EXISTS islamic_content.question_pool (
    pool_id     SERIAL PRIMARY KEY,
    category    VARCHAR(30) NOT NULL,   -- typeahead_how | typeahead_what | ... | daily
    question    TEXT NOT NULL,
    language    VARCHAR(10) DEFAULT 'en',
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qp_category
    ON islamic_content.question_pool (category, is_active);

-- Message feedback (thumbs up/down + detailed feedback)
CREATE TABLE IF NOT EXISTS islamic_content.message_feedback (
    feedback_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(64) NOT NULL,
    session_id      VARCHAR(100) NOT NULL,
    message_index   INT,
    feedback_type   VARCHAR(16) NOT NULL,   -- thumbs_up | thumbs_down
    reason          VARCHAR(30),            -- incorrect | not_asked | slow | style | safety | other
    comment         TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fb_session
    ON islamic_content.message_feedback (session_id);

CREATE INDEX IF NOT EXISTS idx_fb_user
    ON islamic_content.message_feedback (tenant_id, user_id, created_at DESC);

-- Shared message snapshots (for H5 share pages)
CREATE TABLE IF NOT EXISTS islamic_content.shared_messages (
    share_id        VARCHAR(16) PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL,
    user_id         VARCHAR(64) NOT NULL,
    session_id      VARCHAR(100) NOT NULL,
    message_index   INT NOT NULL,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    agent_name      VARCHAR(50) DEFAULT 'Sheikh Wahda',
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_share_expires
    ON islamic_content.shared_messages (expires_at)
    WHERE expires_at IS NOT NULL;
