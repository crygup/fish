-- Persistent Twitch and AniList notifications.
--
-- A row represents one followed entity and one delivery destination.  A
-- subscription may therefore be delivered to more than one guild channel.
-- Guild follows have ``guild_id`` set; follows created in a DM have
-- ``user_id`` set and a NULL ``announce_channel_id`` so the event worker can
-- DM that user.  Keeping these tables separate from the legacy
-- ``twitch_follows`` table lets old text commands continue to operate while
-- the notify command is rolled out.

CREATE TABLE IF NOT EXISTS notify_twitch_follows (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    user_id BIGINT,
    channel_name TEXT NOT NULL,
    -- Existing legacy follows may not have been resolved to a Twitch ID yet;
    -- the notification worker fills this in during its next sync.
    broadcaster_id TEXT,
    announce_channel_id BIGINT,
    mention_role_id BIGINT,
    mention_everyone BOOLEAN NOT NULL DEFAULT FALSE,
    last_stream_id TEXT,
    last_live_at TIMESTAMP WITH TIME ZONE,
    last_offline_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT notify_twitch_owner_check
        CHECK ((guild_id IS NOT NULL) <> (user_id IS NOT NULL)),
    CONSTRAINT notify_twitch_channel_name_check
        CHECK (length(btrim(channel_name)) BETWEEN 1 AND 25),
    CONSTRAINT notify_twitch_mention_check
        CHECK (NOT (mention_role_id IS NOT NULL AND mention_everyone))
);

-- Channel names are case-insensitive on Twitch.  Coalescing the nullable
-- destination makes a DM destination (NULL) unique as well as channel
-- destinations, without relying on PostgreSQL's NULLS NOT DISTINCT syntax.
CREATE UNIQUE INDEX IF NOT EXISTS notify_twitch_guild_key_idx
    ON notify_twitch_follows (
        guild_id,
        lower(btrim(channel_name)),
        COALESCE(announce_channel_id, 0)
    )
    WHERE guild_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS notify_twitch_user_key_idx
    ON notify_twitch_follows (
        user_id,
        lower(btrim(channel_name)),
        COALESCE(announce_channel_id, 0)
    )
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notify_twitch_broadcaster_idx
    ON notify_twitch_follows (broadcaster_id);

CREATE INDEX IF NOT EXISTS notify_twitch_guild_idx
    ON notify_twitch_follows (guild_id)
    WHERE guild_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notify_twitch_user_idx
    ON notify_twitch_follows (user_id)
    WHERE user_id IS NOT NULL;

-- Preserve existing guild follows for the new notification commands.  The
-- insert is intentionally idempotent and leaves unresolved broadcaster IDs
-- NULL so the normal Twitch EventSub sync can resolve them safely.
INSERT INTO notify_twitch_follows
    (guild_id, channel_name, broadcaster_id, announce_channel_id,
     created_at, updated_at)
SELECT
    legacy.guild_id,
    legacy.channel_name,
    legacy.broadcaster_id,
    legacy.announce_channel_id,
    now(),
    now()
FROM twitch_follows AS legacy
WHERE NOT EXISTS (
    SELECT 1
    FROM notify_twitch_follows AS current
    WHERE current.guild_id = legacy.guild_id
      AND lower(btrim(current.channel_name)) = lower(btrim(legacy.channel_name))
      AND COALESCE(current.announce_channel_id, 0)
          = COALESCE(legacy.announce_channel_id, 0)
)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS notify_anime_follows (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT,
    user_id BIGINT,
    anilist_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    site_url TEXT,
    banner_url TEXT,
    official_site_url TEXT,
    crunchyroll_url TEXT,
    announce_channel_id BIGINT,
    mention_role_id BIGINT,
    mention_everyone BOOLEAN NOT NULL DEFAULT FALSE,
    release_at TIMESTAMP WITH TIME ZONE,
    next_airing_at TIMESTAMP WITH TIME ZONE,
    next_episode INTEGER,
    last_checked_at TIMESTAMP WITH TIME ZONE,
    last_notified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT notify_anime_owner_check
        CHECK ((guild_id IS NOT NULL) <> (user_id IS NOT NULL)),
    CONSTRAINT notify_anime_id_check
        CHECK (anilist_id > 0),
    CONSTRAINT notify_anime_title_check
        CHECK (length(btrim(title)) BETWEEN 1 AND 500),
    CONSTRAINT notify_anime_episode_check
        CHECK (next_episode IS NULL OR next_episode > 0),
    CONSTRAINT notify_anime_mention_check
        CHECK (NOT (mention_role_id IS NOT NULL AND mention_everyone))
);

CREATE UNIQUE INDEX IF NOT EXISTS notify_anime_guild_key_idx
    ON notify_anime_follows (
        guild_id,
        anilist_id,
        COALESCE(announce_channel_id, 0)
    )
    WHERE guild_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS notify_anime_user_key_idx
    ON notify_anime_follows (
        user_id,
        anilist_id,
        COALESCE(announce_channel_id, 0)
    )
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notify_anime_guild_idx
    ON notify_anime_follows (guild_id)
    WHERE guild_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notify_anime_user_idx
    ON notify_anime_follows (user_id)
    WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS notify_anime_airing_idx
    ON notify_anime_follows (next_airing_at)
    WHERE next_airing_at IS NOT NULL;

-- Keep one compact scope-count query available to command implementations.
-- The command layer still performs the count and insert in one transaction;
-- these indexes make the count inexpensive and the limits explicit here.
COMMENT ON TABLE notify_twitch_follows IS
    'Notify follows: maximum 10 distinct Twitch channels per guild or user scope.';
COMMENT ON TABLE notify_anime_follows IS
    'Notify follows: maximum 20 distinct AniList entries per guild or user scope.';
