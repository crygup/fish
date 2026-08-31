-- Consolidate the user tracking controls while keeping currency opt-out
-- separate from the general history categories.  Currency features persist
-- wallets, transactions, and wager history, so an explicit opt-out must be
-- enforced before those commands run.
ALTER TABLE user_settings
    ADD COLUMN IF NOT EXISTS currency_tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- Existing guild history was historically private in an earlier migration,
-- but server privacy is now public by default.  Preserve an administrator's
-- explicit opt-out rows while making every configured guild visible unless it
-- was deliberately disabled via guild_opted_out.
ALTER TABLE guild_settings
    ALTER COLUMN history_public SET DEFAULT TRUE;

UPDATE guild_settings
SET history_public = TRUE
WHERE history_public IS DISTINCT FROM TRUE;
