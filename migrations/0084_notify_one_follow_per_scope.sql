-- Keep one notification row per followed entity and server/DM scope.
-- Channel routing is a property of the follow, not a second subscription.

-- Existing versions allowed one row per destination channel.  Keep the most
-- recently updated row (which is the destination most recently selected) and
-- remove older duplicates before replacing those indexes.
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY guild_id, lower(btrim(channel_name))
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS rn
    FROM notify_twitch_follows
    WHERE guild_id IS NOT NULL
)
DELETE FROM notify_twitch_follows AS follows
USING ranked
WHERE follows.id = ranked.id
  AND ranked.rn > 1;

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY user_id, lower(btrim(channel_name))
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS rn
    FROM notify_twitch_follows
    WHERE user_id IS NOT NULL
)
DELETE FROM notify_twitch_follows AS follows
USING ranked
WHERE follows.id = ranked.id
  AND ranked.rn > 1;

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY guild_id, anilist_id
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS rn
    FROM notify_anime_follows
    WHERE guild_id IS NOT NULL
)
DELETE FROM notify_anime_follows AS follows
USING ranked
WHERE follows.id = ranked.id
  AND ranked.rn > 1;

WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY user_id, anilist_id
            ORDER BY updated_at DESC NULLS LAST, id DESC
        ) AS rn
    FROM notify_anime_follows
    WHERE user_id IS NOT NULL
)
DELETE FROM notify_anime_follows AS follows
USING ranked
WHERE follows.id = ranked.id
  AND ranked.rn > 1;

-- The legacy Twitch table and the notify table share a follow.  If both rows
-- exist, use the surviving notify destination for both records so the event
-- worker cannot announce the same stream in two channels.
UPDATE twitch_follows AS legacy
SET announce_channel_id = current.announce_channel_id,
    broadcaster_id = COALESCE(current.broadcaster_id, legacy.broadcaster_id)
FROM notify_twitch_follows AS current
WHERE current.guild_id = legacy.guild_id
  AND lower(btrim(current.channel_name)) = lower(btrim(legacy.channel_name))
  AND current.announce_channel_id IS NOT NULL;

DROP INDEX IF EXISTS notify_twitch_guild_key_idx;
DROP INDEX IF EXISTS notify_twitch_user_key_idx;
DROP INDEX IF EXISTS notify_anime_guild_key_idx;
DROP INDEX IF EXISTS notify_anime_user_key_idx;

CREATE UNIQUE INDEX notify_twitch_guild_key_idx
    ON notify_twitch_follows (guild_id, lower(btrim(channel_name)))
    WHERE guild_id IS NOT NULL;

CREATE UNIQUE INDEX notify_twitch_user_key_idx
    ON notify_twitch_follows (user_id, lower(btrim(channel_name)))
    WHERE user_id IS NOT NULL;

CREATE UNIQUE INDEX notify_anime_guild_key_idx
    ON notify_anime_follows (guild_id, anilist_id)
    WHERE guild_id IS NOT NULL;

CREATE UNIQUE INDEX notify_anime_user_key_idx
    ON notify_anime_follows (user_id, anilist_id)
    WHERE user_id IS NOT NULL;

COMMENT ON TABLE notify_twitch_follows IS
    'Notify follows: maximum 10 distinct Twitch channels per guild or DM scope; one row per followed channel and scope.';
COMMENT ON TABLE notify_anime_follows IS
    'Notify follows: maximum 20 distinct AniList entries per guild or DM scope; one row per followed anime and scope.';
