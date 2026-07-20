-- Orphaned inventory cannot be reached by the application. Remove it before
-- enforcing ownership and cascade future account deletions.
DELETE FROM fishing_rods child
WHERE NOT EXISTS (
    SELECT 1 FROM fishing_accounts parent WHERE parent.user_id = child.user_id
);
DELETE FROM fishing_bait child
WHERE NOT EXISTS (
    SELECT 1 FROM fishing_accounts parent WHERE parent.user_id = child.user_id
);
DELETE FROM fishing_catches child
WHERE NOT EXISTS (
    SELECT 1 FROM fishing_accounts parent WHERE parent.user_id = child.user_id
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fishing_rods_account_fk') THEN
        ALTER TABLE fishing_rods ADD CONSTRAINT fishing_rods_account_fk
            FOREIGN KEY (user_id) REFERENCES fishing_accounts(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fishing_bait_account_fk') THEN
        ALTER TABLE fishing_bait ADD CONSTRAINT fishing_bait_account_fk
            FOREIGN KEY (user_id) REFERENCES fishing_accounts(user_id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fishing_catches_account_fk') THEN
        ALTER TABLE fishing_catches ADD CONSTRAINT fishing_catches_account_fk
            FOREIGN KEY (user_id) REFERENCES fishing_accounts(user_id) ON DELETE CASCADE;
    END IF;
END $$;

UPDATE twitch_eventsub_events SET status = 'pending'
WHERE status NOT IN ('pending', 'processing', 'done', 'dead');
UPDATE twitch_announcement_deliveries SET status = 'pending'
WHERE status NOT IN ('pending', 'processing', 'done', 'dead');

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'twitch_eventsub_events_status_check') THEN
        ALTER TABLE twitch_eventsub_events ADD CONSTRAINT twitch_eventsub_events_status_check
            CHECK (status IN ('pending', 'processing', 'done', 'dead'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'twitch_announcement_deliveries_status_check') THEN
        ALTER TABLE twitch_announcement_deliveries ADD CONSTRAINT twitch_announcement_deliveries_status_check
            CHECK (status IN ('pending', 'processing', 'done', 'dead'));
    END IF;
END $$;
