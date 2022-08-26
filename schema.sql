CREATE TABLE IF NOT EXISTS message_logs (
    author_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    message_content TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    deleted BOOLEAN,
    has_attachments BOOLEAN
);

CREATE TABLE IF NOT EXISTS message_attachment_logs (
    message_id BIGINT,
    attachment BYTEA,
    deleted BOOLEAN
);

CREATE TABLE IF NOT EXISTS avatar_logs (
    user_id BIGINT,
    avatar BYTEA,
    format TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_avatar_logs (
    user_id BIGINT,
    avatar BYTEA,
    format TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS username_logs (
    user_id BIGINT,
    username TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS discrim_logs (
    user_id BIGINT,
    discrim TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS nickname_logs (
    user_id BIGINT,
    guild_id BIGINT,
    nickname TEXT,
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
    member_id BIGINT,
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT,
    auto_download BIGINT,
    poketwo BOOLEAN,
    auto_reactions BOOLEAN,
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