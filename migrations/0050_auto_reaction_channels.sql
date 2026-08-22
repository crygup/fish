-- Allow automatic reactions to be scoped to multiple channels.
-- An enabled guild with no rows is intentionally server-wide.

CREATE TABLE IF NOT EXISTS guild_auto_reaction_channels (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS guild_auto_reaction_channels_channel_idx
    ON guild_auto_reaction_channels (channel_id, guild_id);

-- Preserve existing one-channel configurations when moving to the new table.
INSERT INTO guild_auto_reaction_channels (guild_id, channel_id)
SELECT guild_id, auto_reactions_channel
FROM guild_settings
WHERE auto_reactions_channel IS NOT NULL
ON CONFLICT (guild_id, channel_id) DO NOTHING;
