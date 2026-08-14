-- Store opt-out-aware personal bests for endless streak games.
CREATE TABLE IF NOT EXISTS streak_game_stats (
    game TEXT NOT NULL CHECK (game IN ('higher_or_lower', 'heads_or_tails')),
    user_id BIGINT NOT NULL,
    highest_streak BIGINT NOT NULL DEFAULT 0 CHECK (highest_streak >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (game, user_id)
);

CREATE INDEX IF NOT EXISTS streak_game_stats_leaderboard_idx
    ON streak_game_stats (game, highest_streak DESC, updated_at, user_id);
