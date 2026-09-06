-- Persist character claims detected from Mudae spawn-message edits.
-- The guild switch defaults to enabled so existing and newly joined servers
-- receive the feature without an extra setup step.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS mudae_recent_claims BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS mudae_recent_claims (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    character_name TEXT NOT NULL CHECK (length(btrim(character_name)) > 0),
    claiming_username TEXT NOT NULL DEFAULT 'Unknown',
    claiming_user_id BIGINT NOT NULL DEFAULT 1,
    claimed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    event_key TEXT NOT NULL DEFAULT ''
);

-- A message may be divorced and claimed again later.  De-duplicate one
-- gateway edit while allowing multiple claim revisions for the same message.
ALTER TABLE mudae_recent_claims
    ADD COLUMN IF NOT EXISTS event_key TEXT;

UPDATE mudae_recent_claims
SET event_key = CONCAT('legacy-', id)
WHERE event_key IS NULL OR event_key = '';

ALTER TABLE mudae_recent_claims
    ALTER COLUMN event_key SET NOT NULL;

ALTER TABLE mudae_recent_claims
    DROP CONSTRAINT IF EXISTS mudae_recent_claims_guild_id_message_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS mudae_recent_claims_event_idx
    ON mudae_recent_claims (guild_id, message_id, event_key);

CREATE INDEX IF NOT EXISTS mudae_recent_claims_guild_time_idx
    ON mudae_recent_claims (guild_id, claimed_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS mudae_recent_claims_claimant_idx
    ON mudae_recent_claims (guild_id, claiming_user_id);
