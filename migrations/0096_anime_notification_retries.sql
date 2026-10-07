-- Keep due deliveries independent of AniList's advancing episode schedule.
ALTER TABLE notify_anime_follows
    ADD COLUMN pending_notifications jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(pending_notifications) = 'object');
