-- Extend YouTube notification follows to support personal (DM) subscriptions
-- and per-follow event/mention configuration from the ``notify`` hybrid group.
--
-- The website's guild-only API keeps working: existing rows have ``guild_id``
-- set and ``user_id`` NULL, and the new ``user_id``/mention columns default to
-- NULL/FALSE so those inserts need no change.  A follow is either guild-scoped
-- or user-scoped, never both, and the row keeps the existing channel-keyed
-- WebSub subscription lifecycle.

-- The old primary key (guild_id, youtube_channel_id) makes ``guild_id`` NOT
-- NULL, which cannot represent a DM follow.  Replace it with a UNIQUE
-- constraint (NULLs are distinct, so DM rows pass through) that keeps the
-- website's ``ON CONFLICT (guild_id, youtube_channel_id)`` inserts working.
ALTER TABLE youtube_follows
    DROP CONSTRAINT IF EXISTS youtube_follows_pkey;

ALTER TABLE youtube_follows
    ALTER COLUMN guild_id DROP NOT NULL,
    ALTER COLUMN announce_channel_id DROP NOT NULL;

ALTER TABLE youtube_follows
    ADD CONSTRAINT youtube_follows_guild_channel_key
    UNIQUE (guild_id, youtube_channel_id);

ALTER TABLE youtube_follows
    ADD COLUMN IF NOT EXISTS user_id BIGINT,
    ADD COLUMN IF NOT EXISTS mention_role_id BIGINT,
    ADD COLUMN IF NOT EXISTS mention_everyone BOOLEAN NOT NULL DEFAULT FALSE;

-- The ``notify`` group addresses follows by a stable public ID (mirroring
-- ``notify_twitch_follows``/``notify_anime_follows``).  Add an identity
-- column and make it the primary key.
ALTER TABLE youtube_follows
    ADD COLUMN IF NOT EXISTS id BIGINT GENERATED ALWAYS AS IDENTITY;
ALTER TABLE youtube_follows
    ADD CONSTRAINT youtube_follows_pkey PRIMARY KEY (id);

-- One personal follow per (user, channel).
CREATE UNIQUE INDEX IF NOT EXISTS youtube_follows_user_channel_uniq
    ON youtube_follows (user_id, youtube_channel_id)
    WHERE user_id IS NOT NULL;

ALTER TABLE youtube_follows
    ADD CONSTRAINT youtube_follows_owner_check
    CHECK ((guild_id IS NOT NULL) <> (user_id IS NOT NULL));

ALTER TABLE youtube_follows
    ADD CONSTRAINT youtube_follows_mention_check
    CHECK (NOT (mention_role_id IS NOT NULL AND mention_everyone));

-- Delivery dedup must now work for DM destinations too.  The old primary
-- key (guild_id, youtube_channel_id, item_id, event_type) cannot represent a
-- DM row, so drop it before relaxing the NOT NULL and add the user_id column.
ALTER TABLE youtube_announcement_deliveries
    DROP CONSTRAINT IF EXISTS youtube_announcement_deliveries_pkey;

ALTER TABLE youtube_announcement_deliveries
    ALTER COLUMN guild_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS user_id BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS youtube_announcement_delivery_guild_key
    ON youtube_announcement_deliveries (guild_id, youtube_channel_id, item_id, event_type)
    WHERE guild_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS youtube_announcement_delivery_user_key
    ON youtube_announcement_deliveries (user_id, youtube_channel_id, item_id, event_type)
    WHERE user_id IS NOT NULL;
