CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT NOT NULL,
    last_fm TEXT,
    steam TEXT,
    roblox TEXT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    expires TIMESTAMP,
    created TIMESTAMP DEFAULT (now() at time zone 'utc'),
    event TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE INDEX IF NOT EXISTS reminders_expires_idx ON reminders (expires);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY, -- The discord user ID
    timezone TEXT -- The user's timezone
);

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';

CREATE TABLE IF NOT EXISTS plonks (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    entity_id BIGINT UNIQUE
);

CREATE INDEX IF NOT EXISTS plonks_guild_id_idx ON plonks (guild_id);
CREATE INDEX IF NOT EXISTS plonks_entity_id_idx ON plonks (entity_id);

CREATE TABLE IF NOT EXISTS command_config (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT,
    name TEXT,
    whitelist BOOLEAN
);

CREATE INDEX IF NOT EXISTS command_config_guild_id_idx ON command_config (guild_id);

CREATE TABLE IF NOT EXISTS avatars (
    id SERIAL,
    user_id BIGINT,
    avatar_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    avatar TEXT,
    PRIMARY KEY(user_id, avatar_key)
);

CREATE TABLE IF NOT EXISTS guild_avatars (
    id SERIAL,
    member_id BIGINT,
    guild_id BIGINT,
    avatar_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    avatar TEXT,
    PRIMARY KEY(member_id, avatar_key, guild_id)
);

CREATE TABLE IF NOT EXISTS guild_icons (
    id SERIAL,
    guild_id BIGINT,
    icon_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    icon TEXT,
    PRIMARY KEY(icon_key, guild_id)
);

CREATE TABLE IF NOT EXISTS username_logs (
    id SERIAL,
    user_id BIGINT,
    username TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS discrim_logs (
    id SERIAL,
    user_id BIGINT,
    discrim TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS nickname_logs (
    id SERIAL,
    user_id BIGINT,
    guild_id BIGINT,
    nickname TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_name_logs (
    id SERIAL,
    guild_id BIGINT,
    name TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);