-- Persistent global wins and losses for the LastLetter game.
CREATE TABLE IF NOT EXISTS lastletter_stats (
    user_id BIGINT PRIMARY KEY,
    wins BIGINT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses BIGINT NOT NULL DEFAULT 0 CHECK (losses >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lastletter_stats_leaderboard_idx
    ON lastletter_stats (wins DESC, losses ASC, updated_at, user_id);
