-- Allow users to own multiple titles while selecting one visible profile title.

ALTER TABLE user_titles
    ADD COLUMN IF NOT EXISTS equipped BOOLEAN NOT NULL DEFAULT FALSE;

-- Preserve the title that was most recently purchased before equipping was
-- introduced.  The partial unique index below requires at most one equipped
-- active title per user.
WITH newest AS (
    SELECT DISTINCT ON (user_id) user_id, title_key
    FROM user_titles
    WHERE active
    ORDER BY user_id, purchased_at DESC, title_key ASC
)
UPDATE user_titles AS titles
SET equipped = TRUE
FROM newest
WHERE titles.user_id = newest.user_id
  AND titles.title_key = newest.title_key
  AND titles.active;

CREATE UNIQUE INDEX IF NOT EXISTS user_titles_one_equipped_idx
    ON user_titles (user_id)
    WHERE active AND equipped;
