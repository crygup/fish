-- Keep only an expiring pseudonymous marker after account deletion.
CREATE TABLE reward_cooldowns (
    subject_hash text PRIMARY KEY,
    eligible_at timestamptz NOT NULL
);
CREATE INDEX reward_cooldowns_expiry_idx ON reward_cooldowns (eligible_at);

-- JSON owns only the rows it has explicitly synchronized. Database-only
-- offers remain editable without being overwritten or retired by the seeds.
ALTER TABLE title_catalog ADD COLUMN catalog_managed boolean NOT NULL DEFAULT false;
ALTER TABLE badge_catalog ADD COLUMN catalog_managed boolean NOT NULL DEFAULT false;
ALTER TABLE color_catalog ADD COLUMN catalog_managed boolean NOT NULL DEFAULT false;
ALTER TABLE ring_catalog ADD COLUMN catalog_managed boolean NOT NULL DEFAULT false;
