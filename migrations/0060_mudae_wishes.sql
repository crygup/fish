-- Persist per-server Mudae wish filters so notifications survive restarts.
CREATE TABLE IF NOT EXISTS mudae_wishes (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    wish_type TEXT NOT NULL CHECK (wish_type IN ('character', 'series', 'kakera')),
    wish_value TEXT NOT NULL CHECK (length(trim(wish_value)) > 0),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id, wish_type, wish_value)
);

CREATE INDEX IF NOT EXISTS mudae_wishes_guild_idx
    ON mudae_wishes (guild_id);

CREATE INDEX IF NOT EXISTS mudae_wishes_user_idx
    ON mudae_wishes (user_id);
