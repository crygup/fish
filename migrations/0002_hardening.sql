-- Integrity guarantees introduced after the original schema.
DELETE FROM pinboard_pins older
USING pinboard_pins newer
WHERE older.message_id = newer.message_id AND older.ctid > newer.ctid;
CREATE UNIQUE INDEX IF NOT EXISTS pinboard_pins_message_idx
    ON pinboard_pins (message_id);
-- A reputation user is one logical row. Preserve the aggregate before enforcing it.
WITH combined AS (
    SELECT user_id, SUM(COALESCE(count, 0)) AS count, MIN(id) AS keep_id
    FROM user_rep
    GROUP BY user_id
), updated AS (
    UPDATE user_rep target
    SET count = combined.count
    FROM combined
    WHERE target.id = combined.keep_id
)
DELETE FROM user_rep target
USING combined
WHERE target.user_id = combined.user_id AND target.id <> combined.keep_id;

CREATE UNIQUE INDEX IF NOT EXISTS user_rep_user_idx ON user_rep (user_id);

ALTER TABLE web_sessions ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE web_sessions ALTER COLUMN last_seen_at SET DEFAULT now();
ALTER TABLE pokemon_solves ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE roblox_templates ALTER COLUMN cached_at SET DEFAULT now();
ALTER TABLE corn_reacts ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE banned_ips ALTER COLUMN banned_at SET DEFAULT now();
