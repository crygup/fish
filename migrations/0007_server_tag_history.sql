CREATE TABLE IF NOT EXISTS stag_logs (
    id SERIAL,
    user_id BIGINT NOT NULL,
    tag TEXT,
    guild_id BIGINT,
    guild_created_at TIMESTAMP WITH TIME ZONE,
    badge_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS stag_logs_user_created_idx
    ON stag_logs (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS stag_logs_guild_idx
    ON stag_logs (guild_id)
    WHERE guild_id IS NOT NULL;
