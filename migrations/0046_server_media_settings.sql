-- Server media automation destinations and hourly post filters.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload BIGINT;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_images BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_gifs BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_videos BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS guild_hourly_posts (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    images BOOLEAN NOT NULL DEFAULT TRUE,
    gifs BOOLEAN NOT NULL DEFAULT TRUE,
    videos BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS guild_hourly_post_blocks (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    blocked_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS guild_hourly_post_blocks_user_idx
    ON guild_hourly_post_blocks (user_id, guild_id);
