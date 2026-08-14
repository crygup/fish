-- Require an explicit acknowledgement before non-game history is shared.
-- Existing tracking and visibility choices are preserved.  A false value
-- means the user will see the consent prompt the first time they use a
-- tracking-history command.
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS tracking_consent BOOLEAN NOT NULL DEFAULT FALSE;
