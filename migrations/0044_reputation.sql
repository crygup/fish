-- Keep the old reputation aggregates and logs while adding source-aware
-- events for Fishie and imported Tatsu reputation activity.

ALTER TABLE user_rep_logs
    ADD COLUMN IF NOT EXISTS guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS source_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE;

-- Existing legacy rows have no reliable timestamp.  Leave them NULL rather
-- than treating all historical entries as if they happened during migration.
ALTER TABLE user_rep_logs
    ALTER COLUMN created_at SET DEFAULT now();

CREATE TABLE IF NOT EXISTS reputation_events (
    id BIGSERIAL PRIMARY KEY,
    giver_id BIGINT NOT NULL,
    receiver_id BIGINT,
    guild_id BIGINT,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'guild')),
    source TEXT NOT NULL CHECK (source IN ('fishie', 'tatsu')),
    source_message_id BIGINT,
    period_start DATE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'user' AND receiver_id IS NOT NULL)
        OR (kind = 'guild' AND receiver_id IS NULL AND guild_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS guild_rep (
    guild_id BIGINT PRIMARY KEY,
    count BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS reputation_events_giver_idx
    ON reputation_events (giver_id, created_at);
CREATE INDEX IF NOT EXISTS reputation_events_receiver_idx
    ON reputation_events (receiver_id, created_at);
CREATE INDEX IF NOT EXISTS reputation_events_guild_idx
    ON reputation_events (guild_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS reputation_events_source_message_idx
    ON reputation_events (source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reputation_events_fishie_period_idx
    ON reputation_events (giver_id, kind, period_start)
    WHERE source = 'fishie';
CREATE UNIQUE INDEX IF NOT EXISTS user_rep_logs_source_message_idx
    ON user_rep_logs (source_message_id)
    WHERE source_message_id IS NOT NULL;
