CREATE TABLE IF NOT EXISTS reaction_tracking (
    user_id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reaction_logs (
    id BIGSERIAL PRIMARY KEY,
    giver_id BIGINT NOT NULL,
    receiver_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    unicode BOOLEAN NOT NULL DEFAULT FALSE,
    user_created_at TIMESTAMP WITH TIME ZONE,
    guild_created_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reaction_logs_guild_emoji_idx
    ON reaction_logs (guild_id, emoji_name, emoji_id, unicode);

CREATE INDEX IF NOT EXISTS reaction_logs_giver_idx
    ON reaction_logs (giver_id);

CREATE INDEX IF NOT EXISTS reaction_logs_receiver_idx
    ON reaction_logs (receiver_id);

CREATE INDEX IF NOT EXISTS reaction_logs_guild_giver_idx
    ON reaction_logs (guild_id, giver_id);

CREATE INDEX IF NOT EXISTS reaction_logs_guild_receiver_idx
    ON reaction_logs (guild_id, receiver_id);

CREATE UNIQUE INDEX IF NOT EXISTS reaction_logs_message_giver_emoji_idx
    ON reaction_logs (
        message_id,
        giver_id,
        emoji_name,
        COALESCE(emoji_id, 0),
        unicode
    );
