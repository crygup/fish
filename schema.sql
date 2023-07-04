CREATE TABLE IF NOT EXISTS avatars (
    id SERIAL,
    user_id BIGINT,
    avatar_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    avatar TEXT,
    PRIMARY KEY(user_id, avatar_key)
);

CREATE TABLE IF NOT EXISTS opted_out (
    user_id BIGINT,
    items TEXT[],
    PRIMARY KEY (user_id)
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

CREATE TABLE IF NOT EXISTS command_logs (
    user_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    command TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS uptime_logs (
    user_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS member_join_logs (
    id SERIAL,
    member_id BIGINT,
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT,
    auto_download BIGINT,
    poketwo BOOLEAN DEFAULT FALSE,
    auto_reactions BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (guild_id)
);

CREATE TABLE IF NOT EXISTS guild_prefixes (
    guild_id BIGINT,
    prefix TEXT,
    author_id BIGINT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (guild_id, prefix)
);

CREATE TABLE IF NOT EXISTS guild_join_logs (
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_bans (
    guild_id BIGINT,
    mod_id BIGINT,
    target_id BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_kicks (
    guild_id BIGINT,
    mod_id BIGINT,
    target_id BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT,
    osu TEXT,
    lastfm TEXT,
    steam TEXT,
    roblox TEXT,
    genshin TEXT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL,
    guild_id BIGINT,
    author_id BIGINT,
    name TEXT,
    content TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    uses BIGINT DEFAULT 0,
    PRIMARY KEY (guild_id, name)
);

CREATE TABLE IF NOT EXISTS table_boosters (
    user_id BIGINT,
    role_id BIGINT,
    author_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (user_id)
);


CREATE TABLE IF NOT EXISTS block_list (
    snowflake BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (snowflake)
);

CREATE TABLE IF NOT EXISTS afk (
    user_id BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS nsfw_covers (
    album_id TEXT,
    PRIMARY KEY (album_id)
);

CREATE TABLE IF NOT EXISTS pokemon_guesses (
    pokemon_name TEXT,
    author_id BIGINT,
    correct BIGINT DEFAULT 0,
    incorrect BIGINT DEFAULT 0
);


CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    expires TIMESTAMP,
    created TIMESTAMP DEFAULT (now() at time zone 'utc'),
    event TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE INDEX IF NOT EXISTS reminders_expires_idx ON reminders (expires);

CREATE TABLE IF NOT EXISTS steam_games (
    app_id BIGINT,
    name TEXT,
    PRIMARY KEY (app_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT,
    fm_autoreact BOOLEAN DEFAULT FALSE,
    mudae_pokemon BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS tatsu_rep_logs (
    id SERIAL,
    user_id BIGINT,
    target_id BIGINT,
    guild_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_blocks (
    id SERIAL,
    author_id BIGINT,
    guild_id BIGINT,
    entitiy_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS messages (
    ID SERIAL,
    message_id BIGINT,
    user_id BIGINT,
    channel_id BIGINT,
    webhook_id BIGINT,
    pinned BOOLEAN,
    edited BOOLEAN,
    deleted BOOLEAN,
    stickers BOOLEAN,
    embeds BOOLEAN,
    attachments BOOLEAN,
    content TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    edited_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    guild_id BIGINT,
    guild_owner_id BIGINT
);

CREATE TABLE IF NOT EXISTS attachments (
    ID SERIAL,
    description TEXT,
    filename TEXT,
    url TEXT,
    proxy_url TEXT,
    size INT,
    height INT,
    width INT,
    attachment_id BIGINT,
    message_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS stickers (
    ID SERIAL,
    description TEXT,
    format TEXT,
    url TEXT,
    sticker_id BIGINT,
    message_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS tatsu_reminders (
    user_id BIGINT,
    PRIMARY KEY (user_id)
);