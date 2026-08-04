CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    author_id BIGINT NOT NULL,
    claimed BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    claimed_at TIMESTAMP WITH TIME ZONE,
    uses BIGINT NOT NULL DEFAULT 0 CHECK (uses >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS tags_guild_name_idx
    ON tags (guild_id, lower(name));

CREATE INDEX IF NOT EXISTS tags_guild_author_idx
    ON tags (guild_id, author_id);

CREATE TABLE IF NOT EXISTS emoji_stats (
    author_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    emoji_id TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    unicode BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS emoji_stats_guild_emoji_idx
    ON emoji_stats (guild_id, emoji_id, unicode);

CREATE INDEX IF NOT EXISTS emoji_stats_author_emoji_idx
    ON emoji_stats (author_id, emoji_id, unicode);

CREATE INDEX IF NOT EXISTS emoji_stats_created_at_idx
    ON emoji_stats (created_at);
