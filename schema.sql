CREATE TABLE IF NOT EXISTS message_logs (
    author_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    message_content TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    snipe BOOLEAN
);

CREATE TABLE IF NOT EXISTS message_attachment_logs (
    message_id BIGINT,
    attachment BYTEA,
    snipe BOOLEAN
);

CREATE TABLE IF NOT EXISTS snipe_logs (
    author_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    message_content TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS snipe_attachment_logs (
    message_id BIGINT,
    attachment BYTEA
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

CREATE TABLE IF NOT EXISTS guild_join_logs (
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_blacklist (
    guild_id BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (guild_id)
);

CREATE TABLE IF NOT EXISTS user_blacklist (
    user_id BIGINT,
    reason TEXT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (user_id)
);