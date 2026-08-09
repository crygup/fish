CREATE TABLE IF NOT EXISTS channel_locks (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    had_overwrite BOOLEAN NOT NULL,
    allow_bits BIGINT NOT NULL DEFAULT 0,
    deny_bits BIGINT NOT NULL DEFAULT 0,
    locked_by BIGINT NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS channel_locks_guild_idx
    ON channel_locks (guild_id);
