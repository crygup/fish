-- Preserve the original last-seen cache before introducing status history.
CREATE TABLE IF NOT EXISTS user_statuses_legacy_backup
    (LIKE user_statuses INCLUDING ALL);

INSERT INTO user_statuses_legacy_backup (user_id, guild_id, status, last_seen)
SELECT user_id, guild_id, status, last_seen
FROM user_statuses
ON CONFLICT (user_id, guild_id, status) DO NOTHING;

CREATE TABLE IF NOT EXISTS user_status_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    ended_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT user_status_history_status_check
        CHECK (status IN ('offline', 'idle', 'online', 'dnd')),
    CONSTRAINT user_status_history_interval_check
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS user_status_history_active_idx
    ON user_status_history (user_id, guild_id)
    WHERE ended_at IS NULL;

CREATE INDEX IF NOT EXISTS user_status_history_user_guild_started_idx
    ON user_status_history (user_id, guild_id, started_at DESC);

CREATE INDEX IF NOT EXISTS user_status_history_retention_idx
    ON user_status_history (ended_at)
    WHERE ended_at IS NOT NULL;

-- Seed the new history from the most recent timestamp retained for each legacy
-- status. The newest legacy state remains open so the next presence event can
-- close it normally.
WITH ordered AS (
    SELECT
        user_id,
        guild_id,
        status,
        last_seen AS started_at,
        lead(last_seen) OVER (
            PARTITION BY user_id, guild_id
            ORDER BY last_seen, status
        ) AS ended_at
    FROM user_statuses
)
INSERT INTO user_status_history (
    user_id,
    guild_id,
    status,
    started_at,
    ended_at,
    created_at
)
SELECT
    user_id,
    guild_id,
    status,
    started_at,
    ended_at,
    started_at
FROM ordered
WHERE NOT EXISTS (SELECT 1 FROM user_status_history);
