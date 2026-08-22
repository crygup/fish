CREATE TABLE IF NOT EXISTS accounts (
    user_id BIGINT NOT NULL,
    lastfm TEXT,
    lastfm_session_key TEXT,
    steam TEXT,
    roblox TEXT,
    genshin TEXT,
    letterboxd TEXT,
    spotify TEXT,
    spotify_refresh_token TEXT,
    anilist TEXT,
    anilist_access_token TEXT,
    PRIMARY KEY (user_id)
);

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS letterboxd TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS lastfm_session_key TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS spotify TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS spotify_refresh_token TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS anilist TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS anilist_access_token TEXT;

CREATE TABLE IF NOT EXISTS web_sessions (
    session_id_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    discord_access_token TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (now() at time zone 'utc'),
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (now() at time zone 'utc')
);

CREATE INDEX IF NOT EXISTS web_sessions_expires_at_idx ON web_sessions (expires_at);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS oauth_states_expires_at_idx ON oauth_states (expires_at);

CREATE TABLE IF NOT EXISTS reminders (
    id SERIAL PRIMARY KEY,
    expires TIMESTAMP,
    created TIMESTAMP DEFAULT (now() at time zone 'utc'),
    event TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE INDEX IF NOT EXISTS reminders_expires_idx ON reminders (expires);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id BIGINT PRIMARY KEY,
    timezone TEXT,
    anilist_default_media TEXT NOT NULL DEFAULT 'anime'
);

ALTER TABLE reminders ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS anilist_default_media TEXT NOT NULL DEFAULT 'anime';

CREATE TABLE IF NOT EXISTS user_statuses (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, status)
);

ALTER TABLE user_statuses
    ALTER COLUMN last_seen SET DEFAULT now();

CREATE TABLE IF NOT EXISTS plonks (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    entity_id BIGINT UNIQUE
);

CREATE INDEX IF NOT EXISTS plonks_guild_id_idx ON plonks (guild_id);
CREATE INDEX IF NOT EXISTS plonks_entity_id_idx ON plonks (entity_id);

CREATE TABLE IF NOT EXISTS command_config (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    channel_id BIGINT,
    name TEXT,
    whitelist BOOLEAN
);

CREATE INDEX IF NOT EXISTS command_config_guild_id_idx ON command_config (guild_id);

CREATE TABLE IF NOT EXISTS command_disables (
    guild_id BIGINT NOT NULL,
    command TEXT NOT NULL,
    channel_id BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, command, channel_id)
);

CREATE INDEX IF NOT EXISTS command_disables_guild_channel_idx
    ON command_disables (guild_id, channel_id);

CREATE TABLE IF NOT EXISTS avatars (
    id SERIAL,
    user_id BIGINT,
    avatar_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    avatar TEXT,
    PRIMARY KEY(user_id, avatar_key)
);

CREATE TABLE IF NOT EXISTS guild_avatars (
    id SERIAL,
    member_id BIGINT,
    guild_id BIGINT,
    avatar_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    avatar TEXT,
    PRIMARY KEY(member_id, avatar_key, guild_id)
);

CREATE TABLE IF NOT EXISTS guild_icons (
    id SERIAL,
    guild_id BIGINT,
    icon_key TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    icon TEXT,
    PRIMARY KEY(icon_key, guild_id)
);

CREATE TABLE IF NOT EXISTS username_logs (
    id SERIAL,
    user_id BIGINT,
    username TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS display_name_logs (
    id SERIAL,
    user_id BIGINT,
    display_name TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS discrim_logs (
    id SERIAL,
    user_id BIGINT,
    discrim TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS nickname_logs (
    id SERIAL,
    user_id BIGINT,
    guild_id BIGINT,
    nickname TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_name_logs (
    id SERIAL,
    guild_id BIGINT,
    name TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS opted_out (
    user_id BIGINT,
    items TEXT[],
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS guild_opted_out (
    guild_id BIGINT,
    items TEXT[],
    PRIMARY KEY (guild_id)
);

CREATE TABLE IF NOT EXISTS member_join_logs (
    id SERIAL,
    member_id BIGINT,
    guild_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS guild_join_logs (
    guild_id BIGINT,
    owner_id BIGINT,
    time TIMESTAMP WITH TIME ZONE
);

ALTER TABLE guild_join_logs ADD COLUMN IF NOT EXISTS owner_id BIGINT;

CREATE TABLE IF NOT EXISTS guild_prefixes (
    guild_id BIGINT,
    prefix TEXT,
    author_id BIGINT,
    time TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (guild_id, prefix)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT,
    auto_download BIGINT,
    auto_upload BIGINT,
    auto_upload_images BOOLEAN NOT NULL DEFAULT TRUE,
    auto_upload_gifs BOOLEAN NOT NULL DEFAULT TRUE,
    auto_upload_videos BOOLEAN NOT NULL DEFAULT TRUE,
    poketwo BOOLEAN DEFAULT FALSE,
    poketwo_channel BIGINT,
    auto_reactions BOOLEAN DEFAULT FALSE,
    auto_reactions_channel BIGINT,
    pinboard BIGINT,
    PRIMARY KEY (guild_id)
);

-- Optional channel targets for automatic reactions.  When a guild has the
-- feature enabled and no rows here, reactions apply server-wide.
CREATE TABLE IF NOT EXISTS guild_auto_reaction_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS guild_auto_reaction_channels_channel_idx
    ON guild_auto_reaction_channels (channel_id, guild_id);

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS dehoist BOOLEAN DEFAULT FALSE;

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload BIGINT;

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_images BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_gifs BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_upload_videos BOOLEAN NOT NULL DEFAULT TRUE;

-- Destination and media filters for hourly library posts.  Keeping these in
-- their own table avoids adding a collection of nullable fields to the
-- general server settings row and makes a missing destination unambiguous.
CREATE TABLE IF NOT EXISTS guild_hourly_posts (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    images BOOLEAN NOT NULL DEFAULT TRUE,
    gifs BOOLEAN NOT NULL DEFAULT TRUE,
    videos BOOLEAN NOT NULL DEFAULT TRUE,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    next_post_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
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

CREATE TABLE IF NOT EXISTS guild_log_channels (
    guild_id BIGINT NOT NULL,
    event TEXT NOT NULL,
    channel_id BIGINT NOT NULL,
    webhook_url TEXT,
    PRIMARY KEY (guild_id, event)
);

ALTER TABLE guild_log_channels
    ADD COLUMN IF NOT EXISTS webhook_url TEXT;

CREATE INDEX IF NOT EXISTS guild_log_channels_guild_idx
    ON guild_log_channels (guild_id);

CREATE TABLE IF NOT EXISTS highlights (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    word TEXT NOT NULL,
    word_normalized TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id, word_normalized)
);

CREATE INDEX IF NOT EXISTS highlights_guild_idx
    ON highlights (guild_id);

CREATE TABLE IF NOT EXISTS twitch_follows (
    guild_id BIGINT NOT NULL,
    channel_name TEXT NOT NULL,
    announce_channel_id BIGINT NOT NULL,
    message_template TEXT,
    broadcaster_id TEXT,
    last_stream_id TEXT,
    PRIMARY KEY (guild_id, channel_name)
);

CREATE TABLE IF NOT EXISTS twitch_eventsub_subscriptions (
    broadcaster_id TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twitch_eventsub_events (
    message_id TEXT PRIMARY KEY,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error TEXT
);

ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS payload JSONB;
ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();
ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();
ALTER TABLE twitch_eventsub_events ADD COLUMN IF NOT EXISTS last_error TEXT;
UPDATE twitch_eventsub_events SET status = 'done' WHERE payload IS NULL;

CREATE INDEX IF NOT EXISTS twitch_eventsub_events_pending_idx
    ON twitch_eventsub_events (next_attempt_at)
    WHERE status IN ('pending', 'processing');

CREATE TABLE IF NOT EXISTS twitch_announcement_deliveries (
    guild_id BIGINT NOT NULL,
    channel_name TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error TEXT,
    PRIMARY KEY (guild_id, channel_name, stream_id)
);

CREATE INDEX IF NOT EXISTS twitch_follows_guild_idx
    ON twitch_follows (guild_id);

CREATE TABLE IF NOT EXISTS pokemon_guesses (
    pokemon_name TEXT,
    author_id BIGINT,
    correct BIGINT DEFAULT 0,
    incorrect BIGINT DEFAULT 0,
    PRIMARY KEY (pokemon_name, author_id)
);

CREATE TABLE IF NOT EXISTS command_logs (
    user_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT,
    message_id BIGINT,
    command TEXT,
    created_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS message_xp (
    id SERIAL,
    user_id BIGINT,
    messages BIGINT,
    xp BIGINT,
    PRIMARY KEY (user_id)  
);

CREATE TABLE IF NOT EXISTS user_rep (
    id SERIAL,
    user_id BIGINT,
    count INT
);

-- Owner-managed userinfo badges.  The badge key leaves room for multiple
-- achievement badges per user while preserving the current custom badge.
CREATE TABLE IF NOT EXISTS user_badges (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    emoji_name TEXT NOT NULL,
    emoji_id BIGINT,
    is_custom BOOLEAN NOT NULL,
    unicode BOOLEAN NOT NULL DEFAULT FALSE,
    animated BOOLEAN NOT NULL DEFAULT FALSE,
    badge_key TEXT NOT NULL DEFAULT 'custom',
    text TEXT NOT NULL,
    CHECK (
        (is_custom AND emoji_id IS NOT NULL AND NOT unicode)
        OR (NOT is_custom AND emoji_id IS NULL AND unicode)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS user_badges_user_key_idx
    ON user_badges (user_id, badge_key);
CREATE INDEX IF NOT EXISTS user_badges_user_idx
    ON user_badges (user_id, id DESC);

CREATE TABLE IF NOT EXISTS user_rep_logs (
    id SERIAL,
    user_id BIGINT,
    author_id BIGINT,
    value BOOLEAN,
    comment TEXT,
    guild_id BIGINT,
    source_message_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Reputation events retain the giver, recipient, scope, and source so daily
-- and weekly limits can be enforced without losing the legacy aggregates.
CREATE TABLE IF NOT EXISTS reputation_events (
    id BIGSERIAL PRIMARY KEY,
    giver_id BIGINT NOT NULL,
    receiver_id BIGINT,
    guild_id BIGINT,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'guild')),
    source TEXT NOT NULL CHECK (source IN ('fishie', 'tatsu')),
    source_message_id BIGINT,
    period_start DATE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CHECK (
        (kind = 'user' AND receiver_id IS NOT NULL)
        OR (kind = 'guild' AND receiver_id IS NULL AND guild_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS guild_rep (
    guild_id BIGINT PRIMARY KEY,
    count BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS reputation_events_giver_idx
    ON reputation_events (giver_id, created_at);
CREATE INDEX IF NOT EXISTS reputation_events_receiver_idx
    ON reputation_events (receiver_id, created_at);
CREATE INDEX IF NOT EXISTS reputation_events_guild_idx
    ON reputation_events (guild_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS reputation_events_source_message_idx
    ON reputation_events (source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reputation_events_fishie_period_idx
    ON reputation_events (giver_id, kind, period_start)
    WHERE source = 'fishie';
CREATE UNIQUE INDEX IF NOT EXISTS user_rep_logs_source_message_idx
    ON user_rep_logs (source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS pinboard_pins (
    message_id BIGINT,
    author_id BIGINT,
    target_id BIGINT,
    guild_id BIGINT,
    channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS user_fishing (
    user_id BIGINT,
    rod_level INT,
    fish_caught BIGINT,
    coins BIGINT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS caught_fish (
    user_id BIGINT,
    bass BIGINT,
    commandtuna BIGINT,
    salmon BIGINT,
    caught_fish BIGINT,
    carp BIGINT,
    trout BIGINT,
    sardine BIGINT,
    blue_tang BIGINT,
    pike BIGINT,
    mackerel BIGINT,
    red_snapper BIGINT,
    shark BIGINT,
    hammerhead_shark BIGINT,
    great_white_shark BIGINT,
    leviathan BIGINT,
    kraken BIGINT,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS sold_fish (
    user_id BIGINT,
    bass BIGINT,
    commandtuna BIGINT,
    salmon BIGINT,
    caught_fish BIGINT,
    carp BIGINT,
    trout BIGINT,
    sardine BIGINT,
    blue_tang BIGINT,
    pike BIGINT,
    mackerel BIGINT,
    red_snapper BIGINT,
    shark BIGINT,
    hammerhead_shark BIGINT,
    great_white_shark BIGINT,
    leviathan BIGINT,
    kraken BIGINT,
    PRIMARY KEY (user_id)
);

-- Fishing catalog data lives in files/data/fishing.json. These tables only
-- contain player state and inventory so catalog balancing does not require a
-- database migration.
CREATE TABLE IF NOT EXISTS fishing_accounts (
    user_id BIGINT PRIMARY KEY,
    coins BIGINT NOT NULL DEFAULT 20 CHECK (coins >= 0),
    equipped_rod_key TEXT,
    equipped_rod_rarity_key TEXT,
    equipped_bait_key TEXT,
    total_catches BIGINT NOT NULL DEFAULT 0 CHECK (total_catches >= 0)
);

CREATE TABLE IF NOT EXISTS fishing_rods (
    user_id BIGINT NOT NULL,
    rod_key TEXT NOT NULL,
    rarity_key TEXT NOT NULL,
    quantity BIGINT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (user_id, rod_key, rarity_key)
);

CREATE TABLE IF NOT EXISTS fishing_bait (
    user_id BIGINT NOT NULL,
    bait_key TEXT NOT NULL,
    quantity BIGINT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (user_id, bait_key)
);

CREATE TABLE IF NOT EXISTS fishing_catches (
    user_id BIGINT NOT NULL,
    creature_key TEXT NOT NULL,
    rarity_key TEXT NOT NULL,
    quantity BIGINT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    PRIMARY KEY (user_id, creature_key, rarity_key)
);

CREATE TABLE IF NOT EXISTS added_pokemon (
    name TEXT,
    created_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS pokemon_solves (
    id SERIAL,
    user_id BIGINT NOT NULL,
    pokemon_name TEXT NOT NULL,
    method TEXT NOT NULL,
    guild_id BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE TABLE IF NOT EXISTS roblox_templates (
    asset_id BIGINT PRIMARY KEY,
    image_url TEXT NOT NULL,
    item_name TEXT NOT NULL DEFAULT '',
    extra JSONB DEFAULT ('{}'::jsonb),
    cached_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE TABLE IF NOT EXISTS mudae_timers (
    guild_id BIGINT NOT NULL,
    item TEXT NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (guild_id, item)
);

CREATE TABLE IF NOT EXISTS mudae_subs (
    guild_id BIGINT NOT NULL,
    item TEXT NOT NULL,
    user_ids BIGINT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (guild_id, item)
);

CREATE TABLE IF NOT EXISTS mudae_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS mudae_dm_consent (
    user_id BIGINT PRIMARY KEY,
    consented BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS phone_consent (
    user_id BIGINT PRIMARY KEY,
    consented BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS honeypot_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_template TEXT NOT NULL DEFAULT 'This channel was made to catch people who spam in every channel, if you type here there will be no coming back.'
);

CREATE TABLE IF NOT EXISTS corn_reacts (
    id SERIAL PRIMARY KEY,
    receiver_id BIGINT NOT NULL,
    giver_id BIGINT NOT NULL,
    guild_id BIGINT,
    message_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE INDEX IF NOT EXISTS corn_reacts_receiver_idx ON corn_reacts (receiver_id);
CREATE INDEX IF NOT EXISTS corn_reacts_giver_idx ON corn_reacts (giver_id);
CREATE INDEX IF NOT EXISTS corn_reacts_guild_idx ON corn_reacts (guild_id);

-- Enforce one corn reaction per giver/message across restarts and cache expiry.
DELETE FROM corn_reacts older
USING corn_reacts newer
WHERE older.giver_id = newer.giver_id
  AND older.message_id = newer.message_id
  AND older.id > newer.id;

CREATE UNIQUE INDEX IF NOT EXISTS corn_reacts_giver_message_idx
    ON corn_reacts (giver_id, message_id);

CREATE TABLE IF NOT EXISTS banned_ips (
    ip TEXT PRIMARY KEY,
    banned_at TIMESTAMP WITH TIME ZONE DEFAULT (now() at time zone 'utc')
);

CREATE TABLE IF NOT EXISTS ror2_items (
    id SERIAL,
    internal_name TEXT PRIMARY KEY,
    name TEXT,
    desc_short TEXT,
    desc_full TEXT,
    rarity TEXT,
    categories TEXT[] DEFAULT '{}',
    achievement_locked TEXT,
    stats JSONB DEFAULT ('{}'::jsonb),
    lore TEXT,
    desc_full_info TEXT,
    corrupted_iname TEXT,
    extra JSONB DEFAULT ('{}'::jsonb)
);

CREATE TABLE IF NOT EXISTS ror2_enemies (
    id SERIAL,
    internal_name TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    lore TEXT,
    stats JSONB DEFAULT ('{}'::jsonb),
    extra JSONB DEFAULT ('{}'::jsonb),
    img_url TEXT
);
