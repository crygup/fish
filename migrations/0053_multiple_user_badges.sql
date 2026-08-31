-- Allow each user to have more than one owner-managed custom badge.
-- Existing rows receive stable keys derived from their surrogate IDs before
-- replacing the old one-badge-per-user index.
UPDATE user_badges
SET badge_key = 'custom:' || id::text
WHERE badge_key = 'custom';

DROP INDEX IF EXISTS user_badges_user_key_idx;

CREATE UNIQUE INDEX IF NOT EXISTS user_badges_user_key_idx
    ON user_badges (user_id, badge_key);
