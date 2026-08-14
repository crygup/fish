-- Game history is public by default. Existing values are preserved so a user
-- who already chose private history is not changed by this migration.
ALTER TABLE user_settings
    ALTER COLUMN game_history_public SET DEFAULT TRUE;
