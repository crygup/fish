CREATE TABLE IF NOT EXISTS lucky_roll_stats (
    user_id BIGINT PRIMARY KEY,
    wins BIGINT NOT NULL DEFAULT 0 CHECK (wins >= 0),
    losses BIGINT NOT NULL DEFAULT 0 CHECK (losses >= 0),
    coins_earned BIGINT NOT NULL DEFAULT 0 CHECK (coins_earned >= 0),
    coins_lost BIGINT NOT NULL DEFAULT 0 CHECK (coins_lost >= 0),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS lucky_roll_stats_leaderboard_idx
    ON lucky_roll_stats (wins DESC, coins_earned DESC, updated_at, user_id);
