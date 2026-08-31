-- Per-server starboard and clownboard settings.  A missing row means that
-- board has never been configured; disabled rows retain their settings so a
-- deleted custom emoji can disable a board without discarding its channel.

CREATE TABLE IF NOT EXISTS guild_boards (
    guild_id BIGINT NOT NULL,
    board_type TEXT NOT NULL CHECK (board_type IN ('starboard', 'clownboard')),
    channel_id BIGINT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    threshold INTEGER NOT NULL DEFAULT 3 CHECK (threshold > 0),
    allow_nsfw BOOLEAN NOT NULL DEFAULT TRUE,
    emoji_name TEXT NOT NULL CHECK (length(emoji_name) > 0),
    emoji_id BIGINT,
    emoji_animated BOOLEAN NOT NULL DEFAULT FALSE,
    emoji_set_by BIGINT,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, board_type),
    CHECK (emoji_id IS NOT NULL OR NOT emoji_animated)
);

-- The two boards in one guild must never listen for the same emoji.  Custom
-- emoji names can change, so their stable Discord IDs are the unique key;
-- Unicode emoji use their literal text.
CREATE UNIQUE INDEX IF NOT EXISTS guild_boards_custom_emoji_unique_idx
    ON guild_boards (guild_id, emoji_id)
    WHERE emoji_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS guild_boards_unicode_emoji_unique_idx
    ON guild_boards (guild_id, emoji_name)
    WHERE emoji_id IS NULL;

CREATE INDEX IF NOT EXISTS guild_boards_channel_idx
    ON guild_boards (channel_id, guild_id);

-- Map each source message to its posted board message so reaction updates are
-- idempotent and can edit an existing post after a process restart.
CREATE TABLE IF NOT EXISTS guild_board_entries (
    guild_id BIGINT NOT NULL,
    board_type TEXT NOT NULL,
    source_channel_id BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    board_channel_id BIGINT NOT NULL,
    board_message_id BIGINT NOT NULL,
    reaction_count INTEGER NOT NULL DEFAULT 0 CHECK (reaction_count >= 0),
    source_author_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, board_type, source_message_id),
    FOREIGN KEY (guild_id, board_type)
        REFERENCES guild_boards (guild_id, board_type)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS guild_board_entries_message_unique_idx
    ON guild_board_entries (board_message_id);

CREATE INDEX IF NOT EXISTS guild_board_entries_source_channel_idx
    ON guild_board_entries (guild_id, source_channel_id, source_message_id);

CREATE TABLE IF NOT EXISTS guild_board_blocks (
    guild_id BIGINT NOT NULL,
    board_type TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('user', 'channel')),
    target_id BIGINT NOT NULL,
    blocked_by BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, board_type, target_type, target_id),
    FOREIGN KEY (guild_id, board_type)
        REFERENCES guild_boards (guild_id, board_type)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS guild_board_blocks_target_idx
    ON guild_board_blocks (target_type, target_id, guild_id);
