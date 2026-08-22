-- Community image and GIF submissions reviewed in the configured post channel.
CREATE TABLE IF NOT EXISTS post_uploads (
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

CREATE INDEX IF NOT EXISTS post_uploads_status_idx
    ON post_uploads (status, id);

CREATE INDEX IF NOT EXISTS post_uploads_uploader_idx
    ON post_uploads (uploader_id, status);

CREATE TABLE IF NOT EXISTS post_upload_blocks (
    user_id BIGINT PRIMARY KEY,
    blocked_by BIGINT NOT NULL,
    blocked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS post_aliases (
    post_id BIGINT NOT NULL REFERENCES post_uploads(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    alias TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, post_id),
    CONSTRAINT post_aliases_alias_length
        CHECK (char_length(btrim(alias)) BETWEEN 1 AND 100)
);

CREATE UNIQUE INDEX IF NOT EXISTS post_aliases_user_alias_idx
    ON post_aliases (user_id, lower(btrim(alias)));

CREATE INDEX IF NOT EXISTS post_aliases_post_idx
    ON post_aliases (post_id);

CREATE TABLE IF NOT EXISTS post_library_blocks (
    user_id BIGINT NOT NULL,
    blocked_uploader_id BIGINT NOT NULL,
    blocked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, blocked_uploader_id),
    CONSTRAINT post_library_blocks_not_self
        CHECK (user_id <> blocked_uploader_id)
);

CREATE INDEX IF NOT EXISTS post_library_blocks_uploader_idx
    ON post_library_blocks (blocked_uploader_id, user_id);

CREATE TABLE IF NOT EXISTS post_library_hides (
    user_id BIGINT NOT NULL,
    post_id BIGINT NOT NULL REFERENCES post_uploads(id) ON DELETE CASCADE,
    hidden_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, post_id)
);

CREATE INDEX IF NOT EXISTS post_library_hides_post_idx
    ON post_library_hides (post_id);

CREATE INDEX IF NOT EXISTS post_uploads_approved_uploader_idx
    ON post_uploads (status, uploader_id, id);
