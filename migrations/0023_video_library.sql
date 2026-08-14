-- Video submissions reviewed in the configured Fishie video channel.
CREATE TABLE IF NOT EXISTS video_uploads (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    review_message_id BIGINT,
    uploader_id BIGINT NOT NULL,
    source_guild_id BIGINT,
    source_channel_id BIGINT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied')),
    approved_by BIGINT,
    denied_by BIGINT,
    denial_reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS video_uploads_status_idx
    ON video_uploads (status, id);

CREATE INDEX IF NOT EXISTS video_uploads_uploader_idx
    ON video_uploads (uploader_id, status);

CREATE TABLE IF NOT EXISTS video_upload_blocks (
    user_id BIGINT PRIMARY KEY,
    blocked_by BIGINT NOT NULL,
    blocked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
