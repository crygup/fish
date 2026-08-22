-- Per-user preferences for the approved Fishie video library.
--
-- Aliases, uploader blocks, and hidden entries belong to the user who created
-- them. They do not change the shared library for anyone else.

CREATE TABLE IF NOT EXISTS video_aliases (
    video_id BIGINT NOT NULL
        REFERENCES video_uploads(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    alias TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, video_id),
    CONSTRAINT video_aliases_alias_length
        CHECK (char_length(btrim(alias)) BETWEEN 1 AND 100)
);

-- Alias lookup is case-insensitive and ignores surrounding whitespace. A
-- unique index also prevents a user from assigning one name to two videos.
CREATE UNIQUE INDEX IF NOT EXISTS video_aliases_user_alias_idx
    ON video_aliases (user_id, lower(btrim(alias)));

CREATE INDEX IF NOT EXISTS video_aliases_video_idx
    ON video_aliases (video_id);

CREATE TABLE IF NOT EXISTS video_library_blocks (
    user_id BIGINT NOT NULL,
    blocked_uploader_id BIGINT NOT NULL,
    blocked_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, blocked_uploader_id),
    CONSTRAINT video_library_blocks_not_self
        CHECK (user_id <> blocked_uploader_id)
);

CREATE INDEX IF NOT EXISTS video_library_blocks_uploader_idx
    ON video_library_blocks (blocked_uploader_id, user_id);

CREATE TABLE IF NOT EXISTS video_library_hides (
    user_id BIGINT NOT NULL,
    video_id BIGINT NOT NULL
        REFERENCES video_uploads(id) ON DELETE CASCADE,
    hidden_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, video_id)
);

CREATE INDEX IF NOT EXISTS video_library_hides_video_idx
    ON video_library_hides (video_id);

-- Random-pool and leaderboard queries both filter by approval status first.
CREATE INDEX IF NOT EXISTS video_uploads_approved_uploader_idx
    ON video_uploads (status, uploader_id, id);
