-- Saved activity history is private unless a user explicitly makes it public.
-- Existing rows were created before privacy defaults were introduced, so their
-- implicit public value is reset as part of this one-time privacy migration.
ALTER TABLE user_settings
    ALTER COLUMN history_public SET DEFAULT FALSE;

UPDATE user_settings
SET history_public = FALSE
WHERE history_public IS DISTINCT FROM FALSE;
