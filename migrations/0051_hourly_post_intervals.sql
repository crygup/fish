-- Add selectable repeat intervals to hourly library posts.

ALTER TABLE guild_hourly_posts
    ADD COLUMN IF NOT EXISTS interval_minutes INTEGER NOT NULL DEFAULT 60;

ALTER TABLE guild_hourly_posts
    ADD COLUMN IF NOT EXISTS next_post_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();

UPDATE guild_hourly_posts
SET interval_minutes = 60
WHERE interval_minutes IS NULL OR interval_minutes < 10;

UPDATE guild_hourly_posts
SET next_post_at = now()
WHERE next_post_at IS NULL;
