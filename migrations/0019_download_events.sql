CREATE TABLE IF NOT EXISTS download_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT,
    site TEXT NOT NULL CHECK (site <> ''),
    auto_download BOOLEAN NOT NULL DEFAULT FALSE,
    downloaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS download_events_guild_downloaded_idx
    ON download_events (guild_id, downloaded_at DESC);

CREATE INDEX IF NOT EXISTS download_events_user_downloaded_idx
    ON download_events (user_id, downloaded_at DESC);
