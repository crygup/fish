CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT NOT NULL,
    lastfm TEXT,
    steam TEXT,
    roblox TEXT,
    genshin TEXT,
    letterboxd TEXT,
    PRIMARY KEY (user_id)
);

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS letterboxd TEXT;

CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    expires TIMESTAMP,
    created TIMESTAMP DEFAULT (now() at time zone 'utc'),
    event TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE INDEX IF NOT EXISTS reminders_expires_idx ON reminders (expires);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY,
    timezone TEXT 
);

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';

CREATE TABLE IF NOT EXISTS user_statuses (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc'),
    PRIMARY KEY (user_id, guild_id, status)
);

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

CREATE TABLE IF NOT EXISTS display_name_logs (
    id SERIAL,
    user_id BIGINT,
    display_name TEXT,
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

CREATE TABLE IF NOT EXISTS opted_out (
    user_id BIGINT,
    items TEXT[],
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS guild_opted_out (
    guild_id BIGINT,
    items TEXT[],
    PRIMARY KEY (guild_id)
);

CREATE TABLE IF NOT EXISTS member_join_logs (
    id SERIAL,
    member_id BIGINT,
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_join_logs (
    guild_id BIGINT,
    owner_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

ALTER TABLE guild_join_logs ADD COLUMN IF NOT EXISTS owner_id BIGINT;

CREATE TABLE IF NOT EXISTS guild_prefixes (
    guild_id BIGINT,
    prefix TEXT,
    author_id BIGINT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (guild_id, prefix)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT,
    auto_download BIGINT,
    poketwo BOOLEAN DEFAULT FALSE,
    auto_reactions BOOLEAN DEFAULT FALSE,
    pinboard BIGINT,
    PRIMARY KEY (guild_id)
);

CREATE TABLE IF NOT EXISTS pokemon_guesses (
    pokemon_name TEXT,
    author_id BIGINT,
    correct BIGINT DEFAULT 0,
    incorrect BIGINT DEFAULT 0,
    PRIMARY KEY (pokemon_name, author_id)
);

CREATE TABLE IF NOT EXISTS command_logs (
    user_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    command TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS message_xp (
    id SERIAL,
    user_id BIGINT,
    messages BIGINT,
    xp BIGINT,
    PRIMARY KEY (user_id)  
);

CREATE TABLE IF NOT EXISTS user_rep (
    id SERIAL,
    user_id BIGINT,
    count INT
);

CREATE TABLE IF NOT EXISTS user_rep_logs (
    id SERIAL,
    user_id BIGINT,
    author_id BIGINT,
    value BOOLEAN,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS pinboard_pins (
    message_id BIGINT,
    author_id BIGINT,
    target_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS user_fishing (
    user_id BIGINT,
    rod_level INT,
    fish_caught BIGINT,
    coins BIGINT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS caught_fish (
    user_id BIGINT,
    bass BIGINT,
    commandtuna BIGINT,
    salmon BIGINT,
    caught_fish BIGINT,
    carp BIGINT,
    trout BIGINT,
    sardine BIGINT,
    blue_tang BIGINT,
    pike BIGINT,
    mackerel BIGINT,
    red_snapper BIGINT,
    shark BIGINT,
    hammerhead_shark BIGINT,
    great_white_shark BIGINT,
    leviathan BIGINT,
    kraken BIGINT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS sold_fish (
    user_id BIGINT,
    bass BIGINT,
    commandtuna BIGINT,
    salmon BIGINT,
    caught_fish BIGINT,
    carp BIGINT,
    trout BIGINT,
    sardine BIGINT,
    blue_tang BIGINT,
    pike BIGINT,
    mackerel BIGINT,
    red_snapper BIGINT,
    shark BIGINT,
    hammerhead_shark BIGINT,
    great_white_shark BIGINT,
    leviathan BIGINT,
    kraken BIGINT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS added_pokemon (
    name TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS pokemon_solves (
    id SERIAL,
    user_id BIGINT NOT NULL,
    pokemon_name TEXT NOT NULL,
    method TEXT NOT NULL,
    guild_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE TABLE IF NOT EXISTS roblox_templates (
    asset_id BIGINT PRIMARY KEY,
    image_url TEXT NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    extra JSONB DEFAULT ('{}'::jsonb),
    cached_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE TABLE IF NOT EXISTS mudae_timers (
    guild_id BIGINT NOT NULL,
    item TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (guild_id, item)
);

CREATE TABLE IF NOT EXISTS mudae_subs (
    guild_id BIGINT NOT NULL,
    item TEXT NOT NULL,
    user_ids BIGINT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (guild_id, item)
);

CREATE TABLE IF NOT EXISTS mudae_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS mudae_dm_consent (
    user_id BIGINT PRIMARY KEY,
    consented BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS honeypot_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS corn_reacts (
    id SERIAL PRIMARY KEY,
    receiver_id BIGINT NOT NULL,
    giver_id BIGINT NOT NULL,
    guild_id BIGINT,
    message_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE INDEX IF NOT EXISTS corn_reacts_receiver_idx ON corn_reacts (receiver_id);
CREATE INDEX IF NOT EXISTS corn_reacts_giver_idx ON corn_reacts (giver_id);
CREATE INDEX IF NOT EXISTS corn_reacts_guild_idx ON corn_reacts (guild_id);