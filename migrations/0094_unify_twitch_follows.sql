-- Keep IDs, mentions and destination choices from notify when both stores
-- contain a follow. Preserve the old delivery marker to avoid a fresh alert.
INSERT INTO notify_twitch_follows
    (guild_id, channel_name, broadcaster_id, announce_channel_id, last_stream_id)
SELECT DISTINCT ON (guild_id, lower(btrim(channel_name)))
    guild_id, lower(btrim(channel_name)), broadcaster_id, announce_channel_id, last_stream_id
FROM twitch_follows
ORDER BY guild_id, lower(btrim(channel_name)), channel_name
ON CONFLICT (guild_id, lower(btrim(channel_name))) WHERE guild_id IS NOT NULL
DO UPDATE SET
    broadcaster_id = COALESCE(notify_twitch_follows.broadcaster_id, EXCLUDED.broadcaster_id),
    last_stream_id = COALESCE(notify_twitch_follows.last_stream_id, EXCLUDED.last_stream_id);

DROP TABLE twitch_follows;

ALTER TABLE twitch_announcement_deliveries ADD COLUMN follow_id bigint
    REFERENCES notify_twitch_follows(id) ON DELETE CASCADE;
UPDATE twitch_announcement_deliveries d SET follow_id = n.id
FROM notify_twitch_follows n
WHERE n.guild_id = d.guild_id AND lower(btrim(n.channel_name)) = lower(btrim(d.channel_name));
DELETE FROM twitch_announcement_deliveries WHERE follow_id IS NULL;
-- Older rows may differ only by channel-name capitalization. Keep a completed
-- delivery in preference to a retry when their normalized keys coincide.
DELETE FROM twitch_announcement_deliveries WHERE ctid NOT IN (
    SELECT DISTINCT ON (follow_id, stream_id) ctid
    FROM twitch_announcement_deliveries
    ORDER BY follow_id, stream_id, (status = 'done') DESC, updated_at DESC, attempts DESC
);
ALTER TABLE twitch_announcement_deliveries ALTER COLUMN follow_id SET NOT NULL;
ALTER TABLE twitch_announcement_deliveries DROP CONSTRAINT twitch_announcement_deliveries_pkey;
ALTER TABLE twitch_announcement_deliveries ADD PRIMARY KEY (follow_id, stream_id);
ALTER TABLE twitch_announcement_deliveries DROP COLUMN guild_id, DROP COLUMN channel_name;
