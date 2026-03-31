-- 004_share_conversations.sql
-- Upgrade shared_messages to store full conversation snapshots (ChatGPT-style)

-- Add new columns for full conversation
ALTER TABLE islamic_content.shared_messages
    ADD COLUMN IF NOT EXISTS title VARCHAR(200),
    ADD COLUMN IF NOT EXISTS messages JSONB DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS message_count INT DEFAULT 0;

-- Make old single Q&A columns nullable (backward compat)
ALTER TABLE islamic_content.shared_messages
    ALTER COLUMN question DROP NOT NULL,
    ALTER COLUMN answer DROP NOT NULL,
    ALTER COLUMN message_index DROP NOT NULL;
