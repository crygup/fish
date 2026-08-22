-- Optional channel targets for existing guild-wide automation toggles.

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS poketwo_channel BIGINT;
ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS auto_reactions_channel BIGINT;
