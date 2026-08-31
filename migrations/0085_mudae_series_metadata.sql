-- Add stable identifiers and source metadata for Mudae series wishes.
--
-- The original ``mudae_wishes`` table used the (guild, user, type, value)
-- tuple as its only identifier.  Keep that uniqueness constraint for
-- idempotent wishes, but give every row a stable numeric id so series can be
-- removed by id and old rows can be migrated without changing their value.
CREATE SEQUENCE IF NOT EXISTS mudae_wishes_id_seq AS BIGINT;

ALTER TABLE mudae_wishes
    ADD COLUMN IF NOT EXISTS id BIGINT,
    ADD COLUMN IF NOT EXISTS bundle_id BIGINT,
    ADD COLUMN IF NOT EXISTS bundle_key TEXT,
    ADD COLUMN IF NOT EXISTS bundle_name TEXT,
    ADD COLUMN IF NOT EXISTS bundle_created_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS source_guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS source_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS source_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS source_page INTEGER,
    ADD COLUMN IF NOT EXISTS source_entry INTEGER,
    ADD COLUMN IF NOT EXISTS kakera_threshold BIGINT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();

-- Existing rows were created in their owning guild, which is the best source
-- attribution available before scrape metadata was recorded.  New rows may
-- override this with the guild/channel/message that produced the series.
UPDATE mudae_wishes
SET source_guild_id = guild_id
WHERE source_guild_id IS NULL;

-- Populate ids before making the column required.  The sequence is reset to
-- the current high-water mark so this remains safe if a migration was
-- interrupted after the backfill but before it was recorded.
ALTER TABLE mudae_wishes
    ALTER COLUMN id SET DEFAULT nextval('mudae_wishes_id_seq'::regclass);

ALTER SEQUENCE mudae_wishes_id_seq
    OWNED BY mudae_wishes.id;

UPDATE mudae_wishes
SET id = nextval('mudae_wishes_id_seq'::regclass)
WHERE id IS NULL;

SELECT setval(
    'mudae_wishes_id_seq'::regclass,
    COALESCE(MAX(id), 1),
    MAX(id) IS NOT NULL
)
FROM mudae_wishes;

ALTER TABLE mudae_wishes
    ALTER COLUMN id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS mudae_wishes_id_idx
    ON mudae_wishes (id);

-- ``updated_at`` is intentionally separate from ``created_at`` so a scrape
-- can refresh a bundle/page without losing the first-seen timestamp.
-- ``created_at`` already has a non-null default in migration 0060; retain it
-- for old rows and ensure the new timestamp is initialized consistently.
UPDATE mudae_wishes
SET updated_at = created_at
WHERE updated_at IS DISTINCT FROM created_at;

ALTER TABLE mudae_wishes
    DROP CONSTRAINT IF EXISTS mudae_wishes_wish_type_check;

ALTER TABLE mudae_wishes
    ADD CONSTRAINT mudae_wishes_wish_type_check
    CHECK (wish_type IN ('character', 'series', 'series_kakera', 'kakera'));

ALTER TABLE mudae_wishes
    DROP CONSTRAINT IF EXISTS mudae_wishes_series_kakera_check;

ALTER TABLE mudae_wishes
    ADD CONSTRAINT mudae_wishes_series_kakera_check
    CHECK (wish_type <> 'series_kakera' OR kakera_threshold IS NOT NULL);

ALTER TABLE mudae_wishes
    DROP CONSTRAINT IF EXISTS mudae_wishes_source_page_check;

ALTER TABLE mudae_wishes
    ADD CONSTRAINT mudae_wishes_source_page_check
    CHECK (source_page IS NULL OR source_page > 0);

ALTER TABLE mudae_wishes
    DROP CONSTRAINT IF EXISTS mudae_wishes_source_entry_check;

ALTER TABLE mudae_wishes
    ADD CONSTRAINT mudae_wishes_source_entry_check
    CHECK (source_entry IS NULL OR source_entry > 0);

ALTER TABLE mudae_wishes
    DROP CONSTRAINT IF EXISTS mudae_wishes_kakera_threshold_check;

ALTER TABLE mudae_wishes
    ADD CONSTRAINT mudae_wishes_kakera_threshold_check
    CHECK (kakera_threshold IS NULL OR kakera_threshold >= 0);

-- One switch controls the optional server-side ``$imab`` scraper.  A false
-- default keeps this feature opt-in for existing and newly joined servers.
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS mudae_auto_scrape_series BOOLEAN NOT NULL DEFAULT FALSE;

-- A scraped ``$imab`` bundle is shared by every user in the guild.  Keep it
-- separate from wishes so scraping a page does not create one row per user.
-- ``first_seen_at`` is immutable in the command layer; ``created_at`` is
-- retained as a conventional creation timestamp for generic data tooling.
CREATE TABLE IF NOT EXISTS mudae_series_bundles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    source_guild_id BIGINT,
    source_channel_id BIGINT,
    source_message_id BIGINT,
    bundle_name TEXT NOT NULL CHECK (length(btrim(bundle_name)) > 0),
    bundle_key TEXT NOT NULL CHECK (length(btrim(bundle_key)) > 0),
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    latest_page INTEGER,
    latest_total_pages INTEGER,
    latest_message_id BIGINT,
    UNIQUE (guild_id, bundle_key),
    CONSTRAINT mudae_series_bundles_page_check
        CHECK (latest_page IS NULL OR latest_page > 0),
    CONSTRAINT mudae_series_bundles_total_pages_check
        CHECK (latest_total_pages IS NULL OR latest_total_pages > 0)
);

CREATE INDEX IF NOT EXISTS mudae_series_bundles_source_message_idx
    ON mudae_series_bundles (source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS mudae_series_bundles_guild_idx
    ON mudae_series_bundles (guild_id, bundle_key);

-- Every series listed by a bundle receives a stable id.  ``normalized_name``
-- is the comparison key used by wish matching; ``series_name`` preserves the
-- spelling Mudae displayed to users.
CREATE TABLE IF NOT EXISTS mudae_series (
    id BIGSERIAL PRIMARY KEY,
    bundle_id BIGINT NOT NULL REFERENCES mudae_series_bundles(id) ON DELETE CASCADE,
    series_name TEXT NOT NULL CHECK (length(btrim(series_name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(btrim(normalized_name)) > 0),
    character_count INTEGER,
    source_guild_id BIGINT,
    source_channel_id BIGINT,
    source_message_id BIGINT,
    source_page INTEGER,
    source_entry INTEGER,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (bundle_id, normalized_name),
    CONSTRAINT mudae_series_character_count_check
        CHECK (character_count IS NULL OR character_count >= 0),
    CONSTRAINT mudae_series_source_page_check
        CHECK (source_page IS NULL OR source_page > 0),
    CONSTRAINT mudae_series_source_entry_check
        CHECK (source_entry IS NULL OR source_entry > 0)
);

CREATE INDEX IF NOT EXISTS mudae_series_bundle_idx
    ON mudae_series (bundle_id, source_page, source_entry);

CREATE INDEX IF NOT EXISTS mudae_series_name_idx
    ON mudae_series (normalized_name);

CREATE INDEX IF NOT EXISTS mudae_series_source_message_idx
    ON mudae_series (source_message_id)
    WHERE source_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS mudae_wishes_bundle_idx
    ON mudae_wishes (guild_id, user_id, bundle_key)
    WHERE bundle_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS mudae_wishes_source_message_idx
    ON mudae_wishes (source_message_id)
    WHERE source_message_id IS NOT NULL;
