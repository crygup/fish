-- Add separate privacy controls for game history and guild-owned tracking.
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS game_tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS game_history_public BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS history_public BOOLEAN NOT NULL DEFAULT FALSE;

-- Saved histories are private unless their owner explicitly opts in to sharing.
ALTER TABLE user_settings
    ALTER COLUMN history_public SET DEFAULT FALSE,
    ALTER COLUMN game_history_public SET DEFAULT FALSE;

ALTER TABLE guild_settings
    ALTER COLUMN history_public SET DEFAULT FALSE;

UPDATE user_settings
SET history_public = FALSE,
    game_history_public = FALSE
WHERE history_public IS DISTINCT FROM FALSE
   OR game_history_public IS DISTINCT FROM FALSE;

UPDATE guild_settings
SET history_public = FALSE
WHERE history_public IS DISTINCT FROM FALSE;
