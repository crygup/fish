CREATE TABLE IF NOT EXISTS youtube_follows (
    guild_id BIGINT NOT NULL,
    youtube_channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    channel_handle TEXT,
    announce_channel_id BIGINT NOT NULL,
    message_template TEXT,
    event_types TEXT[] NOT NULL DEFAULT ARRAY['video', 'live', 'short', 'community']::TEXT[],
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, youtube_channel_id),
    CONSTRAINT youtube_follows_event_types_check CHECK (
        cardinality(event_types) > 0
        AND event_types <@ ARRAY['video', 'live', 'short', 'community']::TEXT[]
    )
);

CREATE INDEX IF NOT EXISTS youtube_follows_channel_idx
    ON youtube_follows (youtube_channel_id);

CREATE TABLE IF NOT EXISTS youtube_websub_subscriptions (
    youtube_channel_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS youtube_events (
    event_id TEXT PRIMARY KEY,
    youtube_channel_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error TEXT,
    CONSTRAINT youtube_events_status_check
        CHECK (status IN ('pending', 'processing', 'done', 'dead'))
);

CREATE TABLE IF NOT EXISTS youtube_announcement_deliveries (
    guild_id BIGINT NOT NULL,
    youtube_channel_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error TEXT,
    PRIMARY KEY (guild_id, youtube_channel_id, item_id, event_type),
    CONSTRAINT youtube_announcement_event_type_check
        CHECK (event_type IN ('video', 'live', 'short', 'community')),
    CONSTRAINT youtube_announcement_status_check
        CHECK (status IN ('pending', 'processing', 'done', 'dead'))
);

CREATE TABLE IF NOT EXISTS youtube_community_state (
    youtube_channel_id TEXT PRIMARY KEY,
    latest_post_id TEXT,
    checked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
