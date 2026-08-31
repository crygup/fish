-- fishie: no-transaction
-- Keep the global XP leaderboard from scanning and sorting every user row.
CREATE INDEX CONCURRENTLY IF NOT EXISTS message_xp_leaderboard_idx
    ON message_xp (xp DESC, user_id ASC);
