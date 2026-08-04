ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS history_public BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS first_use_notice_shown BOOLEAN NOT NULL DEFAULT FALSE;

INSERT INTO user_settings (user_id, first_use_notice_shown)
SELECT DISTINCT existing.user_id, TRUE
FROM (
    SELECT user_id FROM command_logs
    UNION
    SELECT user_id FROM accounts
    UNION
    SELECT user_id FROM opted_out
    UNION
    SELECT user_id FROM user_settings
) AS existing
ON CONFLICT (user_id) DO UPDATE
SET first_use_notice_shown = TRUE;
